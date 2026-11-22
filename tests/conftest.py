"""Shared test setup.

Two settings are made before any Food Cat code is imported:
  * SECRET_KEY, because the code refuses to run without one,
  * FOODCAT_VISIBILITY_TIMEOUT=0, so a message that was taken from a queue is available
    again at once. That lets the retry tests run without waiting for real seconds,
  * PASSWORD_ROUNDS=1000, so hashing a password takes a millisecond instead of a fraction of a second.
"""
import os
import threading

os.environ["SECRET_KEY"] = "pytest-only-secret-key-1234567890"
os.environ["FOODCAT_VISIBILITY_TIMEOUT"] = "0"
os.environ["PASSWORD_ROUNDS"] = "1000"  # real passwords use 600,000 rounds, which would make the tests slow for no benefit

import pytest  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from foodcat import alert_lambda, delivery_service, gateway, order_service, payment_service, user_service  # noqa: E402
from foodcat import sqs  # noqa: E402
from foodcat.common import make_token  # noqa: E402

CLIFTON = (24.8138, 67.0300)
GULSHAN = (24.9215, 67.0920)
DHA = (24.8010, 67.0640)  # where Burger Lab is


@pytest.fixture
def folder(tmp_path):
    """A fresh, empty data folder for each test."""
    return str(tmp_path)


def as_user(user_id, role="customer"):
    """The headers the gateway would add after checking a token."""
    return {"X-User-Id": str(user_id), "X-User-Role": role}


def settle(folder, rounds=20):
    """Run the three background workers until they have nothing left to do."""
    total = 0
    for _ in range(rounds):
        handled = (payment_service.process_once(folder) + delivery_service.process_once(folder)
                   + alert_lambda.process_once(folder))
        total += handled
        if not handled:
            break
    return total


def drain(folder, queue_name):
    """Take every message off a queue and return their bodies (for checking what was sent)."""
    queue = sqs.Queue(queue_name, folder)
    bodies = []
    while True:
        batch = queue.receive(10)
        if not batch:
            return bodies
        for message in batch:
            bodies.append(message.body)
            queue.delete(message)


class Stack:
    """The whole system running for real: every service on its own port, and the gateway in front."""

    def __init__(self, folder, login_limit=1000):
        self.folder = folder
        self.servers = {}
        self.urls = {
            "user": [self._serve("user-0", user_service.create_app(folder))],
            "order": [self._serve("order-1", order_service.create_app(folder, instance="order-1")),
                      self._serve("order-2", order_service.create_app(folder, instance="order-2"))],
            "payment": [self._serve("payment-0", payment_service.create_app(folder))],
            "delivery": [self._serve("delivery-0", delivery_service.create_app(folder))],
            "alerts": [self._serve("alerts-0", alert_lambda.create_app(folder))],
        }
        self.app = gateway.create_app(urls=self.urls, login_limit=login_limit, folder=folder)
        self.client = self.app.test_client()

    def _serve(self, key, app):
        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()  # a quick stop
        self.servers[key] = server
        return "http://127.0.0.1:%d" % server.server_port

    def stop(self, key):
        """Switch one service instance off (the next call to it gets 'connection refused')."""
        self.servers[key].shutdown()
        self.servers[key].server_close()

    def close(self):
        for server in self.servers.values():
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass

    def call(self, method, path, token=None, headers=None, **body):
        headers = dict(headers or {})
        if token:
            headers["Authorization"] = "Bearer " + token
        return self.client.open("/api" + path, method=method, json=body or None, headers=headers)

    def join(self, name, role="customer"):
        """Sign up and log in. Returns (token, user id)."""
        email = name.lower().replace(" ", ".") + "@example.com"
        made = self.call("POST", "/user/signup", name=name, email=email, password="secret123", role=role)
        assert made.status_code == 201, made.get_json()
        login = self.call("POST", "/user/login", email=email, password="secret123")
        assert login.status_code == 200, login.get_json()
        return login.get_json()["token"], made.get_json()["id"]

    def rider_online(self, token, name, where):
        reply = self.call("PUT", "/delivery/rider", token, online=True, name=name, lat=where[0], lng=where[1], area="x")
        assert reply.status_code == 200, reply.get_json()

    def menu(self):
        return self.call("GET", "/order/menu").get_json()["restaurants"]

    def order(self, token, card="tok_visa", restaurant="Burger Lab", item="Zinger Burger", qty=1, address="House 1, DHA"):
        place = next(r for r in self.menu() if r["name"] == restaurant)
        item_id = next(i["id"] for i in place["items"] if i["name"] == item)
        return self.call("POST", "/order/orders", token, restaurant_id=place["id"], items=[{"item_id": item_id, "qty": qty}],
                         address=address, card=card)

    def alerts(self, token, **query):
        suffix = "?" + "&".join("%s=%s" % kv for kv in query.items()) if query else ""
        return self.call("GET", "/alerts/inbox" + suffix, token).get_json()["alerts"]


@pytest.fixture
def stack(folder):
    system = Stack(folder)
    yield system
    system.close()


@pytest.fixture
def expired_token():
    return make_token(1, "customer", ttl=-10)
