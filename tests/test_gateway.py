"""The front door: login checks, routing, load balancing, failover, throttling, monitoring."""
import socket

import pytest
import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError, ProtocolError

from foodcat import gateway, sqs

from conftest import Stack


def served_by(reply):
    return reply.headers["X-Served-By"]


# ---------------------------------------------------------------- who may come in

def test_the_menu_is_public_but_orders_need_a_login(stack):
    assert stack.call("GET", "/order/menu").status_code == 200
    assert stack.call("GET", "/order/orders").status_code == 401
    assert stack.call("GET", "/delivery/jobs").status_code == 401
    assert stack.call("GET", "/alerts/inbox").status_code == 401
    assert stack.call("GET", "/user/me").status_code == 401


def test_signup_and_login_are_public(stack):
    token, _ = stack.join("Ayesha")
    assert token


@pytest.mark.parametrize("header", ["Bearer garbage", "Bearer ", "Basic abc123", "garbage", "Bearer a.b"])
def test_broken_tokens_are_refused(stack, header):
    assert stack.call("GET", "/order/orders", headers={"Authorization": header}).status_code == 401


def test_an_expired_token_is_refused(stack, expired_token):
    assert stack.call("GET", "/order/orders", expired_token).status_code == 401


def test_a_client_cannot_pretend_to_be_somebody_else(stack):
    ayesha, ayesha_id = stack.join("Ayesha")
    _, bilal_id = stack.join("Bilal", "rider")
    forged = {"X-User-Id": str(bilal_id), "X-User-Role": "rider"}
    assert stack.call("GET", "/user/me", headers=forged).status_code == 401           # no token: no entry
    me = stack.call("GET", "/user/me", ayesha, headers=forged).get_json()               # a token wins over a forged header
    assert me["id"] == ayesha_id and me["role"] == "customer"


def test_a_customer_cannot_use_rider_features_even_with_a_valid_token(stack):
    token, _ = stack.join("Ayesha")
    assert stack.call("GET", "/delivery/jobs", token).status_code == 403
    assert stack.call("PUT", "/delivery/rider", token, online=True).status_code == 403


# ---------------------------------------------------------------- what is not reachable

@pytest.mark.parametrize("path", [
    "/payment/health", "/payment/charge", "/pizza/menu", "/order/admin/cache/down", "/order/admin",
    "/order/%2e%2e/admin/cache/down", "/order/menu/../admin/cache/down", "/order/.hidden", "/order/menu.json",
    "/order/menu%00", "/order/a b", "/user/login/", "/user//login",
])
def test_internal_and_odd_urls_are_not_found(stack, path):
    token, _ = stack.join("Ayesha")
    assert stack.call("POST", path, token).status_code in (404, 405, 308)
    assert stack.call("GET", path, token).status_code in (404, 405, 308)


def test_the_cache_switch_really_is_closed_to_the_outside(stack):
    token, _ = stack.join("Ayesha")
    stack.call("GET", "/order/menu")
    stack.call("POST", "/order/admin/cache/down", token)
    for _ in range(4):
        assert stack.call("GET", "/order/menu").get_json()["source"] in ("cache", "database")   # never "(cache is down)"


# ---------------------------------------------------------------- balancing and failover

def test_calls_take_turns_between_the_two_order_instances(stack):
    names = [served_by(stack.call("GET", "/order/menu")) for _ in range(6)]
    assert names == ["order-1", "order-2"] * 3


def test_when_one_instance_dies_reads_go_to_the_other(stack):
    stack.stop("order-1")
    replies = [stack.call("GET", "/order/menu") for _ in range(5)]
    assert all(r.status_code == 200 and served_by(r) == "order-2" for r in replies)


def test_when_one_instance_dies_new_orders_still_go_through_once(stack):
    token, _ = stack.join("Ayesha")
    stack.stop("order-1")
    for _ in range(3):
        reply = stack.order(token)
        assert reply.status_code == 201 and served_by(reply) == "order-2"
    assert sqs.Queue(sqs.NEW_ORDERS, stack.folder).size() == 3        # three orders, no copies


def test_a_name_that_cannot_be_found_any_more_still_lets_orders_go_to_the_other_instance(stack, monkeypatch):
    """Docker removes the name of a stopped container, so the next call fails at the name lookup. Nothing reached
    the service then, so even a write may safely try the other instance."""
    real_lookup = socket.getaddrinfo

    def lookup(host, *args, **kwargs):
        if host == "order-gone":
            raise socket.gaierror(-2, "Name or service not known")
        return real_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", lookup)
    urls = dict(stack.urls, order=["http://order-gone:5002", stack.urls["order"][0]])
    door = gateway.create_app(urls=urls, folder=stack.folder).test_client()
    token, _ = stack.join("Ayesha")
    place = next(r for r in stack.menu() if r["name"] == "Burger Lab")
    body = dict(restaurant_id=place["id"], items=[{"item_id": place["items"][0]["id"], "qty": 1}], address="House 1, DHA")
    for _ in range(4):
        reply = door.post("/api/order/orders", json=body, headers={"Authorization": "Bearer " + token})
        assert reply.status_code == 201 and served_by(reply) == "order-1"
    assert sqs.Queue(sqs.NEW_ORDERS, stack.folder).size() == 4


def test_only_a_call_that_never_reached_the_service_counts_as_never_connected():
    def wrapped(reason):
        return requests.exceptions.ConnectionError(MaxRetryError(None, "/orders", reason))

    refused = wrapped(NewConnectionError(None, "connection refused"))
    gone = wrapped(NameResolutionError("order-1", None, socket.gaierror(-2, "Name or service not known")))
    cut_off = requests.exceptions.ConnectionError(ProtocolError("Connection aborted.", ConnectionResetError()))
    assert gateway._never_connected(refused)
    assert gateway._never_connected(gone)
    assert gateway._never_connected(requests.exceptions.ConnectTimeout("slow"))
    assert not gateway._never_connected(cut_off)
    assert not gateway._never_connected(requests.exceptions.ConnectionError("no details"))


def test_the_gateway_does_not_send_its_own_traffic_through_a_proxy(stack, monkeypatch):
    """A laptop on a campus or VPN proxy must not break the calls between the gateway and the services."""
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens on port 9
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert stack.call("GET", "/order/menu").status_code == 200


def test_when_every_instance_is_down_the_customer_gets_a_clear_message(stack):
    stack.stop("order-1")
    stack.stop("order-2")
    reply = stack.call("GET", "/order/menu")
    assert reply.status_code == 503 and "try again" in reply.get_json()["error"]


def test_a_service_that_answers_slowly_gives_a_timeout_message(stack, monkeypatch):
    def too_slow(self, method, url, **kwargs):
        raise requests.exceptions.ReadTimeout("slow")

    monkeypatch.setattr(requests.Session, "request", too_slow)
    reply = stack.call("GET", "/order/menu")
    assert reply.status_code == 504


def test_a_write_that_may_have_reached_the_service_is_never_sent_twice(stack, monkeypatch):
    token, _ = stack.join("Ayesha")
    attempts = []

    def dropped(self, method, url, **kwargs):
        attempts.append(url)
        raise requests.exceptions.ConnectionError("connection aborted halfway")   # it may or may not have arrived

    monkeypatch.setattr(requests.Session, "request", dropped)
    reply = stack.call("POST", "/order/orders", token, restaurant_id=1, items=[{"item_id": 1, "qty": 1}], address="x")
    assert reply.status_code == 502 and len(attempts) == 1


def test_service_errors_pass_through_unchanged(stack):
    token, _ = stack.join("Ayesha")
    reply = stack.call("POST", "/order/orders", token, restaurant_id=1, items=[], address="x")
    assert reply.status_code == 400 and "item" in reply.get_json()["error"].lower()


# ---------------------------------------------------------------- protection

def test_too_many_login_attempts_are_blocked(folder):
    system = Stack(folder, login_limit=3)
    try:
        codes = [system.call("POST", "/user/login", email="a@example.com", password="wrong-wrong").status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
    finally:
        system.close()


def test_the_login_limit_is_per_client_not_global(folder):
    system = Stack(folder, login_limit=1)
    try:
        assert system.call("POST", "/user/login", email="a@example.com", password="x").status_code == 401
        assert system.call("POST", "/user/login", email="a@example.com", password="x").status_code == 429
        other = system.app.test_client()
        reply = other.post("/api/user/login", json={"email": "a@example.com", "password": "x"}, environ_overrides={"REMOTE_ADDR": "10.0.0.9"})
        assert reply.status_code == 401
    finally:
        system.close()


def test_a_huge_request_is_stopped_at_the_door(stack):
    reply = stack.client.post("/api/user/signup", data="x" * 100_000, content_type="application/json")
    assert reply.status_code == 413


def test_the_page_and_the_api_carry_safe_headers(stack):
    page = stack.client.get("/")
    assert page.status_code == 200 and b"Food Cat" in page.data
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    api = stack.call("GET", "/order/menu")
    assert api.headers["Cache-Control"] == "no-store" and "Content-Security-Policy" not in api.headers
    script = stack.client.get("/static/app.js")
    assert script.status_code == 200 and b"createTextNode" in script.data


def test_the_page_never_builds_html_from_server_data():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "web" / "app.js").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
    for risky in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert risky not in code


def test_every_file_the_page_asks_for_is_served_and_the_logos_are_real_pictures(stack):
    import re
    from pathlib import Path
    web = Path(__file__).resolve().parent.parent / "web"
    wanted = set()
    for name in ("index.html", "app.js"):
        wanted |= set(re.findall(r"/static/([A-Za-z0-9_.-]+)", (web / name).read_text(encoding="utf-8")))
    assert {"logo.png", "logo-stacked.png", "logo-icon.png", "favicon.png", "style.css", "app.js"} <= wanted
    for name in sorted(wanted):
        reply = stack.client.get("/static/" + name)
        assert reply.status_code == 200, name
        if name.endswith(".png"):
            assert reply.mimetype == "image/png" and reply.data[:8] == b"\x89PNG\r\n\x1a\n", name


def test_the_stylesheet_keeps_the_three_colours_of_the_logo():
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "web" / "style.css").read_text(encoding="utf-8").lower()
    for token in ("--navy: #0e1a38;", "--tangerine: #fe5115;", "--amber: #ff9c25;"):
        assert token in css


def test_files_outside_the_web_folder_cannot_be_fetched(stack):
    assert stack.client.get("/static/../foodcat/common.py").status_code == 404
    assert stack.client.get("/static/%2e%2e/foodcat/common.py").status_code == 404


def test_the_gateway_needs_at_least_one_address_per_service():
    from foodcat import gateway
    with pytest.raises(ValueError):
        gateway.Balancer([])


def test_addresses_come_from_the_environment(monkeypatch):
    from foodcat import gateway
    monkeypatch.setenv("ORDER_URLS", "http://a:1/, http://b:2")
    assert gateway.service_urls()["order"] == ["http://a:1", "http://b:2"]
    assert gateway.service_urls()["user"] == ["http://127.0.0.1:5001"]


# ---------------------------------------------------------------- monitoring

def test_the_monitor_shows_every_service_and_queue(stack):
    report = stack.client.get("/api/monitor").get_json()
    assert {name: [i["status"] for i in items] for name, items in report["services"].items()} == {
        "user": ["up"], "order": ["up", "up"], "payment": ["up"], "delivery": ["up"], "alerts": ["up"]}
    assert report["queues"] == {"new-orders": 0, "paid-orders": 0, "updates": 0, "dead-letter": 0}
    assert sorted(i["instance"] for i in report["services"]["order"]) == ["order-1", "order-2"]


def test_the_monitor_notices_a_dead_instance_and_a_filling_queue(stack):
    stack.stop("order-2")
    sqs.Queue(sqs.DEAD_LETTER, stack.folder).send({"oops": 1})
    report = stack.client.get("/api/monitor").get_json()
    assert sorted(i["status"] for i in report["services"]["order"]) == ["down", "up"]
    assert report["queues"]["dead-letter"] == 1


def test_the_monitor_does_not_leak_internal_addresses(stack):
    text = stack.client.get("/api/monitor").get_data(as_text=True)
    assert "127.0.0.1" not in text and "http://" not in text
