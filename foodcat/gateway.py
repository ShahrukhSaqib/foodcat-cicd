"""API Gateway + Load Balancer: the one front door of Food Cat.

For every request that comes in from the web page or an app, it:
  1. checks the login token (sign-up, login and the menu are open to everyone),
  2. swaps the token for two trusted headers (X-User-Id and X-User-Role), so the services
     behind it never see a password or a token,
  3. picks one instance of the service (round robin) and forwards the call. If that
     instance is down it tries the next one,
  4. adds an X-Served-By header, so you can see which instance answered.

It also serves the web page, a small /api/monitor report (the 'Monitoring' box) and /api/version,
which says which build is running (the pipeline checks it after every deploy).

Only the services in ROUTABLE can be reached from outside. The Payment Service is
deliberately not on that list: nobody can call it, it only listens to a queue.
"""
import itertools
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from flask import Response, jsonify, request, send_from_directory
from urllib3.exceptions import NewConnectionError

from . import sqs
from .common import error, log, make_service, read_token, require_secret, run
from .version import build_info

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Where each service lives. Several addresses (separated by commas) means several instances.
DEFAULT_URLS = {
    "user": "http://127.0.0.1:5001",
    "order": "http://127.0.0.1:5002,http://127.0.0.1:5012",
    "payment": "http://127.0.0.1:5003",
    "delivery": "http://127.0.0.1:5004",
    "alerts": "http://127.0.0.1:5005",
}
ROUTABLE = ("user", "order", "delivery", "alerts")  # payment is not here on purpose
PUBLIC = {("user", "signup"), ("user", "login"), ("order", "menu")}
SAFE_PATH = re.compile(r"[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*")  # no dots, so no '..' tricks
QUIET = {"alerts"}  # the page asks for these every few seconds, so successful calls are not logged


def service_urls():
    """Read the instance lists from the environment: USER_URLS, ORDER_URLS and so on."""
    urls = {}
    for name, default in DEFAULT_URLS.items():
        value = os.environ.get(name.upper() + "_URLS", default)
        urls[name] = [u.strip().rstrip("/") for u in value.split(",") if u.strip()]
    return urls


class Balancer:
    """Round robin: each call starts with the next instance in the list."""

    def __init__(self, urls):
        if not urls:
            raise ValueError("a service needs at least one address")
        self.urls = urls
        self._counter = itertools.count()

    def order(self):
        """All instances, in the order to try them: this turn's one first, then the others."""
        start = next(self._counter) % len(self.urls)
        return self.urls[start:] + self.urls[:start]


class Throttle:
    """Allow at most `limit` hits per `seconds` for each key (used against password guessing)."""

    def __init__(self, limit, seconds=60):
        self.limit = limit
        self.seconds = seconds
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = time.time()
        with self._lock:
            recent = [t for t in self._hits.get(key, []) if t > now - self.seconds]
            allowed = len(recent) < self.limit
            if allowed:
                recent.append(now)
            self._hits[key] = recent
            return allowed


def _never_connected(exc):
    """True when the call could not even reach the service, so nothing was done there and a retry is safe.
    That covers a refused connection, a connect timeout, and a name that cannot be found any more (which is
    what Docker does to the name of a stopped container)."""
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    reason = getattr(exc.args[0], "reason", None) if exc.args else None
    return isinstance(reason, NewConnectionError)


def create_app(urls=None, login_limit=None, folder=None):
    app = make_service("gateway", static_folder=str(WEB_DIR), static_url_path="/static")
    urls = urls or service_urls()
    balancers = {name: Balancer(urls[name]) for name in ROUTABLE}
    if login_limit is None:
        login_limit = int(os.environ.get("LOGIN_LIMIT_PER_MIN", "10"))
    throttle = Throttle(login_limit)
    running = build_info()  # read once: this process keeps serving the build it was started from
    session = requests.Session()
    session.trust_env = False  # our own services are never reached through a proxy, even if the computer has one set

    @app.after_request
    def safe_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def home():
        return send_from_directory(WEB_DIR, "index.html")

    @app.get("/api/version")
    def version():
        """Which build is live. Open to everybody, and it holds nothing secret."""
        return jsonify(running)

    def who_is_calling():
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return None
        return read_token(header[7:].strip())

    @app.route("/api/<service>/<path:rest>", methods=["GET", "POST", "PUT", "DELETE"])
    def forward(service, rest):
        if service not in ROUTABLE or not SAFE_PATH.fullmatch(rest) or rest.split("/")[0] == "admin":
            return error("Not found.", 404)
        if (service, rest) == ("user", "login") and not throttle.allow(request.remote_addr):
            return error("Too many login attempts. Please wait a minute.", 429)

        # Only these headers ever reach a service. Anything a client sent as X-User-Id is dropped here.
        request_id = uuid.uuid4().hex[:8]
        headers = {"X-Request-Id": request_id}
        if request.content_type:
            headers["Content-Type"] = request.content_type
        if (service, rest) not in PUBLIC:
            caller = who_is_calling()
            if caller is None:
                return error("Please log in first.", 401)
            headers["X-User-Id"] = str(caller["uid"])
            headers["X-User-Role"] = str(caller["role"])

        started = time.time()
        body = request.get_data()
        for base in balancers[service].order():
            try:
                reply = session.request(request.method, base + "/" + rest, data=body, headers=headers,
                                        params=list(request.args.items(multi=True)), timeout=(1.5, 10))
            except requests.exceptions.ReadTimeout:
                return error("The service took too long to answer. Please try again.", 504)
            except requests.exceptions.ConnectionError as exc:
                # Reads can always go to the next instance. A write only if it never reached this one.
                if request.method == "GET" or _never_connected(exc):
                    log("gateway", "%s: an instance of %s did not answer, trying the next one" % (request_id, service))
                    continue
                return error("The connection to the service dropped. Please check your order list before trying again.", 502)
            served_by = reply.headers.get("X-Instance", service)
            if service not in QUIET or reply.status_code >= 400:
                log("gateway", "%s %s /api/%s/%s served by %s, status %s (%d ms)" % (
                    request_id, request.method, service, rest, served_by, reply.status_code,
                    (time.time() - started) * 1000))
            response = Response(reply.content, status=reply.status_code,
                                content_type=reply.headers.get("Content-Type", "application/json"))
            response.headers["X-Served-By"] = served_by
            return response
        log("gateway", "%s: no instance of %s is available" % (request_id, service))
        return error("This part of Food Cat is busy or down. Please try again in a moment.", 503)

    @app.get("/api/monitor")
    def monitor():
        """The 'Monitoring' box: is every service alive, and how full are the queues?"""
        def probe(item):
            name, base = item
            try:
                reply = session.get(base + "/health", timeout=1.0)
                return name, {"status": "up", "instance": reply.json().get("instance")}
            except Exception:
                return name, {"status": "down", "instance": None}

        checks = [(name, base) for name, bases in urls.items() for base in bases]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(probe, checks))
        services = {name: [] for name in urls}
        for name, state in results:
            services[name].append(state)
        queues = {}
        for name in (sqs.NEW_ORDERS, sqs.PAID_ORDERS, sqs.UPDATES, sqs.DEAD_LETTER):
            try:
                queues[name] = sqs.Queue(name, folder).size()
            except Exception:
                queues[name] = None
        return jsonify(status="ok", services=services, queues=queues)

    return app


def main():
    require_secret()
    run(create_app(), 8000)


if __name__ == "__main__":
    main()
