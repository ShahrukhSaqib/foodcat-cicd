"""Small helpers that every Food Cat service shares.

Nothing in here knows about orders, payments or riders. It only knows how to:
  * open a service's own SQLite database,
  * make and check login tokens,
  * hash passwords,
  * start a Flask service the same way every time.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException


def data_dir():
    """Folder that holds every database file. Set FOODCAT_DATA_DIR to move it."""
    folder = Path(os.environ.get("FOODCAT_DATA_DIR", "data")).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def queue_dir():
    """The queues live in one file that every service shares (that file plays the role of SQS).
    It gets its own setting, so each service can keep a private data folder in Docker."""
    folder = os.environ.get("FOODCAT_QUEUE_DIR")
    if not folder:
        return data_dir()
    path = Path(folder).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect(name, folder=None):
    """Open one database file. Each service opens only its own file (no shared tables)."""
    path = Path(folder or data_dir()) / (name + ".db")
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    # Switching a brand new file to WAL mode needs a lock that SQLite sometimes refuses at once, without
    # waiting, when several programs open the file in the same moment (Docker starts all the containers in
    # one go). So we simply ask again a moment later.
    for attempt in range(40):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            break
        except sqlite3.OperationalError:
            if attempt == 39:
                conn.close()
                raise
            time.sleep(0.05)
    return conn


@contextmanager
def db(name, folder=None):
    """with db('orders') as conn: ...  (the connection is always closed afterwards)"""
    conn = connect(name, folder)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    """All or nothing. BEGIN IMMEDIATE takes the write lock first, so two workers can't both
    read 'this rider is free' and then both take the same rider."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def log(service, message):
    """One tidy line per business event, so a demo is easy to follow."""
    line = "[%s] %s" % (service, message)
    try:
        print(line, flush=True)
    except UnicodeEncodeError:  # a Windows console that can't show, say, an Urdu name must not break the request
        print(line.encode("ascii", "replace").decode(), flush=True)


# ---------------------------------------------------------------------------
# Passwords and login tokens (standard library only, no extra packages)
# ---------------------------------------------------------------------------

def _rounds():
    """How many times PBKDF2 repeats itself. More rounds make every password guess slower for an
    attacker. 600,000 is what OWASP currently advises for PBKDF2 with SHA 256. Tests turn it down."""
    return int(os.environ.get("PASSWORD_ROUNDS", "600000"))


def hash_password(password, salt=None, rounds=None):
    """Return 'rounds$salt$hash'. The rounds are stored too, so old passwords keep working
    when the number is raised later."""
    salt = salt or os.urandom(16).hex()
    rounds = rounds or _rounds()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds).hex()
    return "%d$%s$%s" % (rounds, salt, digest)


def check_password(password, stored):
    rounds, salt, _ = stored.split("$", 2)
    return hmac.compare_digest(hash_password(password, salt, int(rounds)), stored)


def _secret():
    """The key that signs login tokens. In real life Vault would hand it to each container.
    Here it comes from the environment (the .env file) and there is deliberately no default,
    so nobody can ever run the system with a key that is written in the source code."""
    key = os.environ.get("SECRET_KEY", "")
    if len(key) < 16 or key.startswith("change-me"):
        raise RuntimeError("SECRET_KEY is missing or too weak. Copy .env.example to .env and set a long random value.")
    return key.encode()


def require_secret():
    """Call this at start-up, so a service stops at once instead of failing on the first login."""
    _secret()


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(user_id, role, ttl=3600):
    body = _b64(json.dumps({"uid": user_id, "role": role, "exp": int(time.time()) + ttl}).encode())
    sig = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
    return body + "." + sig


def read_token(token):
    """Return the token's payload, or None when it is forged, broken or expired."""
    try:
        body, sig = token.split(".", 1)
        good = _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, good):
            return None
        payload = json.loads(_unb64(body))
        if payload["exp"] < time.time():
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Flask helpers
# ---------------------------------------------------------------------------

def error(message, status):
    return jsonify(error=message), status


def current_user():
    """The gateway puts the checked user into these two headers. Clients can't fake them
    because the gateway deletes any copy a client sends."""
    uid = request.headers.get("X-User-Id", "")
    if not uid.isdigit():
        return None
    return {"id": int(uid), "role": request.headers.get("X-User-Role", "customer")}


def make_service(name, instance=None, **flask_args):
    """Create a Flask app with a /health route, an X-Instance header and JSON errors."""
    app = Flask(name, **flask_args)
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024  # no request in this system needs to be bigger
    instance = instance or os.environ.get("INSTANCE_NAME", name)

    @app.get("/health")
    def health():
        return jsonify(service=name, instance=instance, status="ok")

    @app.after_request
    def add_instance(response):
        response.headers["X-Instance"] = instance
        return response

    @app.errorhandler(HTTPException)
    def http_problem(exc):
        return jsonify(error=exc.name), exc.code

    @app.errorhandler(Exception)
    def crash(exc):
        log(name, "unexpected error: %r" % (exc,))
        return jsonify(error="Something went wrong on our side."), 500

    return app


def run(app, default_port, worker=None):
    """Start a service. `worker` is an optional background loop (queue consumer)."""
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    if worker:
        threading.Thread(target=worker, daemon=True).start()
    port = int(os.environ.get("PORT", default_port))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port, threaded=True, debug=False)


def loop_forever(step, idle_seconds=0.5):
    """Call step() again and again. step() returns how many messages it handled."""
    while True:
        try:
            handled = step()
        except Exception as exc:  # a worker must never die because of one bad message
            log("worker", "unexpected error: %r" % exc)
            handled = 0
        if not handled:
            time.sleep(idle_seconds)
