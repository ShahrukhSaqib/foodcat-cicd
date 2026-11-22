"""Each service on its own, called directly (the gateway is tested in test_gateway.py)."""
import json
import os
import threading

import pytest

from foodcat import alert_lambda, delivery_service, order_service, payment_service, reports, sqs, user_service
from foodcat.cache import TTLCache
from foodcat.common import db, read_token

from conftest import CLIFTON, DHA, GULSHAN, as_user, drain


# ================================================================ User Service

@pytest.fixture
def users(folder):
    return user_service.create_app(folder).test_client()


def signup(client, **changes):
    body = dict(name="Ayesha", email="ayesha@example.com", password="secret123", role="customer")
    body.update(changes)
    return client.post("/signup", json=body)


def test_signup_then_login_gives_a_token_with_the_role(users):
    assert signup(users, role="rider").status_code == 201
    reply = users.post("/login", json={"email": "ayesha@example.com", "password": "secret123"})
    assert reply.status_code == 200
    payload = read_token(reply.get_json()["token"])
    assert payload["role"] == "rider" and payload["uid"] == reply.get_json()["user"]["id"]


def test_email_is_not_case_sensitive(users):
    signup(users)
    assert users.post("/login", json={"email": "AYESHA@Example.com", "password": "secret123"}).status_code == 200


def test_the_same_email_cannot_sign_up_twice(users):
    signup(users)
    assert signup(users).status_code == 409


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "x" * 41}, {"email": "no-at-sign"}, {"password": "12345"}, {"role": "restaurant"}, {"role": "admin"},
])
def test_bad_signups_are_refused(users, changes):
    assert signup(users, **changes).status_code == 400


def test_the_password_is_never_stored_as_typed(users, folder):
    signup(users)
    with db("users", folder) as conn:
        stored = conn.execute("SELECT password_hash FROM users").fetchone()[0]
    assert "secret123" not in stored


def test_wrong_password_and_unknown_email_look_the_same(users):
    signup(users)
    wrong = users.post("/login", json={"email": "ayesha@example.com", "password": "nope-nope"})
    unknown = users.post("/login", json={"email": "ghost@example.com", "password": "secret123"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json() == unknown.get_json()


def test_me_needs_the_gateway_headers(users):
    user_id = signup(users).get_json()["id"]
    assert users.get("/me").status_code == 401
    assert users.get("/me", headers={"X-User-Id": "abc"}).status_code == 401
    me = users.get("/me", headers=as_user(user_id)).get_json()
    assert me["email"] == "ayesha@example.com" and "password_hash" not in me


def test_unknown_url_and_wrong_method_are_json_errors(users):
    assert users.get("/nothing").get_json() == {"error": "Not Found"}
    assert users.get("/login").status_code == 405


def test_a_huge_request_is_refused(users):
    assert users.post("/signup", data="x" * 100_000, content_type="application/json").status_code == 413


def test_health_says_who_answered(folder):
    reply = user_service.create_app(folder, instance="u-1").test_client().get("/health")
    assert reply.get_json() == {"service": "user-service", "instance": "u-1", "status": "ok"}
    assert reply.headers["X-Instance"] == "u-1"


# ================================================================ Order Service

@pytest.fixture
def orders(folder):
    app = order_service.create_app(folder)
    return app.test_client(), app


def burger_lab(client):
    place = next(r for r in client.get("/menu").get_json()["restaurants"] if r["name"] == "Burger Lab")
    return place, {i["name"]: i["id"] for i in place["items"]}


def test_menu_is_read_from_the_database_first_and_then_from_the_cache(orders):
    client, _ = orders
    assert client.get("/menu").get_json()["source"] == "database"
    assert client.get("/menu").get_json()["source"] == "cache"


def test_every_restaurant_tells_the_page_its_cuisine(orders):
    client, _ = orders
    menu = client.get("/menu").get_json()["restaurants"]
    assert {r["name"]: r["cuisine"] for r in menu} == {"Biryani Junction": "Biryani", "Roll Express": "Rolls", "Burger Lab": "Burgers"}


def test_a_database_from_the_first_version_gets_its_cuisines_instead_of_failing(folder):
    with db("orders", folder) as conn:   # the restaurants table as the first version of Food Cat made it
        conn.execute("CREATE TABLE restaurants (id INTEGER PRIMARY KEY, name TEXT, area TEXT, lat REAL, lng REAL)")
        conn.execute("INSERT INTO restaurants VALUES (3, 'Burger Lab', 'DHA', 24.8010, 67.0640)")
        conn.execute("CREATE TABLE menu_items (id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER, name TEXT, price INTEGER)")
        conn.execute("INSERT INTO menu_items (restaurant_id, name, price) VALUES (3, 'Zinger Burger', 550)")
    client = order_service.create_app(folder).test_client()
    menu = client.get("/menu").get_json()["restaurants"]
    assert [(r["name"], r["cuisine"], len(r["items"])) for r in menu] == [("Burger Lab", "Burgers", 1)]   # kept what was there, added the cuisine


def test_menu_still_works_when_the_cache_is_down(orders):
    client, app = orders
    client.get("/menu")
    app.cache.down = True
    reply = client.get("/menu").get_json()
    assert reply["source"].startswith("database") and len(reply["restaurants"]) == 3


def test_the_cache_switch_works_over_http(orders):
    client, app = orders
    assert client.post("/admin/cache/down").get_json() == {"cache_down": True}
    assert app.cache.down is True
    client.post("/admin/cache/up")
    assert app.cache.down is False
    assert client.post("/admin/cache/sideways").status_code == 400


def test_an_order_is_saved_priced_by_the_server_and_queued(orders, folder):
    client, _ = orders
    place, ids = burger_lab(client)
    reply = client.post("/orders", headers=as_user(5), json={
        "restaurant_id": place["id"], "address": "House 1, DHA", "card": "tok_visa",
        "items": [{"item_id": ids["Zinger Burger"], "qty": 2, "price": 1},   # a cheating client: every price here
                  {"item_id": ids["Cold Coffee"], "qty": 1, "price": 1}],    # and the total below must be ignored
        "price": 1, "total": 1,
    })
    assert reply.status_code == 201
    assert reply.get_json()["total"] == 2 * 550 + 280
    queued = drain(folder, sqs.NEW_ORDERS)
    assert len(queued) == 1
    assert queued[0]["order_id"] == reply.get_json()["order_id"] and queued[0]["amount"] == 1380
    assert queued[0]["user_id"] == 5 and queued[0]["card_token"] == "tok_visa"


def test_placing_an_order_needs_a_login_and_a_customer(orders):
    client, _ = orders
    place, ids = burger_lab(client)
    body = {"restaurant_id": place["id"], "address": "x", "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]}
    assert client.post("/orders", json=body).status_code == 401
    assert client.post("/orders", headers=as_user(1, "rider"), json=body).status_code == 403


@pytest.mark.parametrize("change", [
    {"address": ""}, {"address": "   "}, {"address": "x" * 201}, {"items": []}, {"items": "pizza"}, {"restaurant_id": 99},
    {"items": [{"item_id": 99999, "qty": 1}]}, {"items": [{"item_id": 1}]}, {"items": ["Zinger"]},
])
def test_bad_orders_are_refused(orders, folder, change):
    client, _ = orders
    place, ids = burger_lab(client)
    body = {"restaurant_id": place["id"], "address": "House 1", "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]}
    body.update(change)
    assert client.post("/orders", headers=as_user(1), json=body).status_code == 400
    assert drain(folder, sqs.NEW_ORDERS) == []


@pytest.mark.parametrize("qty", [0, -1, 21, "many"])
def test_quantities_must_be_between_1_and_20(orders, qty):
    client, _ = orders
    place, ids = burger_lab(client)
    reply = client.post("/orders", headers=as_user(1), json={
        "restaurant_id": place["id"], "address": "x", "items": [{"item_id": ids["Zinger Burger"], "qty": qty}]})
    assert reply.status_code == 400


def test_an_item_from_another_restaurant_is_refused(orders):
    client, _ = orders
    place, _ = burger_lab(client)
    biryani = next(r for r in client.get("/menu").get_json()["restaurants"] if r["name"] == "Biryani Junction")
    reply = client.post("/orders", headers=as_user(1), json={
        "restaurant_id": place["id"], "address": "x", "items": [{"item_id": biryani["items"][0]["id"], "qty": 1}]})
    assert reply.status_code == 400


def test_if_the_queue_is_broken_the_customer_is_told_and_nothing_is_charged(orders, monkeypatch):
    client, _ = orders
    place, ids = burger_lab(client)

    def broken(self, body):
        raise OSError("queue unreachable")

    monkeypatch.setattr(sqs.Queue, "send", broken)
    reply = client.post("/orders", headers=as_user(1), json={
        "restaurant_id": place["id"], "address": "x", "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]})
    assert reply.status_code == 503
    mine = client.get("/orders", headers=as_user(1)).get_json()["orders"]
    assert mine[0]["status"] == "FAILED"


def test_people_only_see_their_own_orders(orders):
    client, _ = orders
    place, ids = burger_lab(client)
    body = {"restaurant_id": place["id"], "address": "x", "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]}
    order_id = client.post("/orders", headers=as_user(1), json=body).get_json()["order_id"]
    assert client.get("/orders/%d" % order_id, headers=as_user(1)).status_code == 200
    assert client.get("/orders/%d" % order_id, headers=as_user(2)).status_code == 404
    assert client.get("/orders", headers=as_user(2)).get_json()["orders"] == []
    assert client.get("/orders").status_code == 401


def test_order_service_uses_a_cache_you_hand_it(folder):
    cache = TTLCache(ttl=30)
    client = order_service.create_app(folder, cache=cache).test_client()
    client.get("/menu")
    assert cache.get("menu") is not None


def test_two_order_instances_can_start_in_the_same_moment_on_one_database(folder):
    """order-1 and order-2 share the Order DB, and Docker starts them together. The menu must be added only once."""
    problems = []
    for number in range(40):
        where = os.path.join(folder, "round%d" % number)
        os.mkdir(where)
        gate = threading.Barrier(4)

        def start(where=where, gate=gate):
            gate.wait()
            try:
                order_service.create_app(where)
            except Exception as exc:
                problems.append(repr(exc))

        threads = [threading.Thread(target=start) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        with db("orders", where) as conn:
            assert conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 3
            assert conn.execute("SELECT COUNT(*) FROM menu_items").fetchone()[0] == 12
    assert problems == []


# ================================================================ Payment Service

def new_order(order_id=1, card="tok_visa", user_id=3, amount=550):
    return {"order_id": order_id, "user_id": user_id, "amount": amount, "address": "House 1, DHA",
            "restaurant_id": 3, "restaurant_name": "Burger Lab", "restaurant_lat": DHA[0], "restaurant_lng": DHA[1],
            "card_token": card}


def test_a_good_card_is_paid_and_passed_on_without_the_card_token(folder):
    sqs.Queue(sqs.NEW_ORDERS, folder).send(new_order())
    assert payment_service.process_once(folder) == 1
    paid = drain(folder, sqs.PAID_ORDERS)
    assert len(paid) == 1 and paid[0]["order_id"] == 1
    assert "card_token" not in paid[0] and "amount" not in paid[0]   # Delivery never needs the money details
    updates = drain(folder, sqs.UPDATES)
    assert [u["event"] for u in updates] == ["payment_received"] and updates[0]["details"] == {"amount": 550}
    with db("payments", folder) as conn:
        row = conn.execute("SELECT status, amount FROM payments").fetchone()
    assert (row["status"], row["amount"]) == ("PAID", 550)


def test_a_declined_card_tells_the_customer_and_sends_nothing_to_delivery(folder):
    sqs.Queue(sqs.NEW_ORDERS, folder).send(new_order(card="tok_decline"))
    payment_service.process_once(folder)
    assert drain(folder, sqs.PAID_ORDERS) == []
    assert [u["event"] for u in drain(folder, sqs.UPDATES)] == ["payment_declined"]
    with db("payments", folder) as conn:
        assert conn.execute("SELECT status FROM payments").fetchone()[0] == "DECLINED"


def test_the_same_order_twice_is_charged_once(folder):
    queue = sqs.Queue(sqs.NEW_ORDERS, folder)
    queue.send(new_order(order_id=9))
    queue.send(new_order(order_id=9))      # SQS may deliver a message twice
    payment_service.process_once(folder)
    with db("payments", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_charges").fetchone()[0] == 1


def test_a_crash_after_charging_does_not_charge_again(folder, monkeypatch):
    sqs.Queue(sqs.NEW_ORDERS, folder).send(new_order(order_id=4))
    real_send = sqs.Queue.send
    calls = {"n": 0}

    def crash_once(self, body):
        if self.name == sqs.PAID_ORDERS and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("the server died here")
        return real_send(self, body)

    monkeypatch.setattr(sqs.Queue, "send", crash_once)
    payment_service.process_once(folder)   # charged, then crashes before finishing
    with db("payments", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM provider_charges").fetchone()[0] == 1
    payment_service.process_once(folder)   # the retry finishes the job
    with db("payments", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM provider_charges").fetchone()[0] == 1   # still one single charge
    assert len(drain(folder, sqs.PAID_ORDERS)) == 1


def test_when_the_card_company_is_down_the_order_is_retried_then_parked(folder):
    sqs.Queue(sqs.NEW_ORDERS, folder).send(new_order(card="tok_error"))
    for _ in range(8):
        payment_service.process_once(folder)
    assert sqs.Queue(sqs.NEW_ORDERS, folder).size() == 0
    parked = drain(folder, sqs.DEAD_LETTER)
    assert len(parked) == 1 and "did not answer" in parked[0]["error"]
    with db("payments", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0] == 0   # nobody was charged


def test_payment_service_has_no_public_routes_except_health(folder):
    client = payment_service.create_app(folder).test_client()
    assert client.get("/health").status_code == 200
    assert client.get("/payments").status_code == 404
    assert client.post("/charge", json={"amount": 1}).status_code == 404


# ================================================================ Delivery Service

@pytest.fixture
def delivery(folder):
    return delivery_service.create_app(folder).test_client()


def go_online(client, rider_id, name, where, online=True):
    return client.put("/rider", headers=as_user(rider_id, "rider"),
                      json={"online": online, "name": name, "lat": where[0], "lng": where[1], "area": "x"})


def paid_order(order_id=1):
    order = new_order(order_id)
    for key in ("amount", "card_token", "restaurant_id"):
        order.pop(key)
    return order


def test_the_nearest_free_rider_gets_the_order(folder, delivery):
    go_online(delivery, 10, "Ali", CLIFTON)      # about 3.7 km from Burger Lab
    go_online(delivery, 11, "Sara", GULSHAN)     # about 13 km away
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order())
    assert delivery_service.process_once(folder) == 1
    jobs = delivery.get("/jobs", headers=as_user(10, "rider")).get_json()["jobs"]
    assert len(jobs) == 1 and jobs[0]["status"] == "ASSIGNED" and 3 < jobs[0]["distance_km"] < 5
    assert delivery.get("/jobs", headers=as_user(11, "rider")).get_json()["jobs"] == []
    events = {(u["user_id"], u["event"]) for u in drain(folder, sqs.UPDATES)}
    assert events == {(3, "rider_assigned"), (10, "new_job")}    # the customer and the rider are both told


def test_offline_and_busy_riders_are_skipped(folder, delivery):
    go_online(delivery, 10, "Ali", DHA)
    go_online(delivery, 11, "Sara", GULSHAN)
    go_online(delivery, 10, "Ali", DHA, online=False)           # Ali, the nearest, goes offline
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order(1))
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order(2))
    delivery_service.process_once(folder)
    assert len(delivery.get("/jobs", headers=as_user(11, "rider")).get_json()["jobs"]) == 1   # Sara got order 1
    assert sqs.Queue(sqs.PAID_ORDERS, folder).size() == 1       # nobody is free for order 2, so it waits


def test_an_order_waits_in_the_queue_until_a_rider_appears(folder, delivery):
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order())
    for _ in range(12):                                         # many tries, never a failure
        delivery_service.process_once(folder)
    assert sqs.Queue(sqs.PAID_ORDERS, folder).size() == 1
    assert sqs.Queue(sqs.DEAD_LETTER, folder).size() == 0
    go_online(delivery, 10, "Ali", CLIFTON)
    delivery_service.process_once(folder)
    assert sqs.Queue(sqs.PAID_ORDERS, folder).size() == 0
    assert len(delivery.get("/jobs", headers=as_user(10, "rider")).get_json()["jobs"]) == 1


def test_the_same_paid_message_twice_gives_one_delivery_and_one_rider(folder, delivery):
    go_online(delivery, 10, "Ali", CLIFTON)
    go_online(delivery, 11, "Sara", GULSHAN)
    for _ in range(2):
        sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order(7))
    delivery_service.process_once(folder)
    assert len(delivery.get("/jobs", headers=as_user(10, "rider")).get_json()["jobs"]) == 1
    assert delivery.get("/jobs", headers=as_user(11, "rider")).get_json()["jobs"] == []     # Sara was not also taken
    with db("delivery", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 1
    ids = [u["event_id"] for u in drain(folder, sqs.UPDATES)]
    assert len(ids) == 4 and len(set(ids)) == 2                 # the alerts repeat with the same ids, so Lambda drops the copies


def test_a_rider_walks_a_job_through_pickup_and_delivery_and_is_freed(folder, delivery):
    rider = as_user(10, "rider")
    go_online(delivery, 10, "Ali", CLIFTON)
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order(1))
    delivery_service.process_once(folder)
    assert delivery.get("/rider", headers=rider).get_json()["busy_with"] == 1
    assert delivery.post("/jobs/1/deliver", headers=rider).status_code == 409        # not picked up yet
    assert delivery.post("/jobs/1/pickup", headers=rider).status_code == 200
    assert delivery.post("/jobs/1/pickup", headers=rider).status_code == 409         # cannot pick up twice
    assert go_online(delivery, 10, "Ali", CLIFTON, online=False).status_code == 409  # busy riders cannot leave
    assert delivery.post("/jobs/1/deliver", headers=rider).status_code == 200
    assert delivery.get("/rider", headers=rider).get_json()["busy_with"] is None
    assert go_online(delivery, 10, "Ali", CLIFTON, online=False).status_code == 200
    events = [u["event"] for u in drain(folder, sqs.UPDATES)]
    assert events.count("picked_up") == 1 and events.count("delivered") == 1


def test_riders_cannot_touch_each_others_jobs(folder, delivery):
    go_online(delivery, 10, "Ali", CLIFTON)
    go_online(delivery, 11, "Sara", GULSHAN)
    sqs.Queue(sqs.PAID_ORDERS, folder).send(paid_order(1))
    delivery_service.process_once(folder)
    assert delivery.post("/jobs/1/pickup", headers=as_user(11, "rider")).status_code == 404
    assert delivery.post("/jobs/99/pickup", headers=as_user(10, "rider")).status_code == 404


def test_only_logged_in_riders_may_use_the_rider_api(delivery):
    assert delivery.get("/jobs").status_code == 401
    assert delivery.get("/jobs", headers=as_user(1, "customer")).status_code == 403
    assert delivery.put("/rider", headers=as_user(1, "customer"), json={"online": True}).status_code == 403
    assert delivery.post("/jobs/1/pickup", headers=as_user(1, "customer")).status_code == 403


@pytest.mark.parametrize("body", [
    {}, {"online": "yes"}, {"online": True}, {"online": True, "lat": 91, "lng": 0}, {"online": True, "lat": "north", "lng": 0},
    {"online": True, "lat": 24.8, "lng": 200},
])
def test_going_online_needs_a_clear_answer_and_a_real_location(delivery, body):
    assert delivery.put("/rider", headers=as_user(10, "rider"), json=body).status_code == 400


def test_a_riders_status_when_new_is_offline(delivery):
    assert delivery.get("/rider", headers=as_user(10, "rider")).get_json() == {"online": False, "busy_with": None}


def test_the_rider_name_falls_back_and_is_cut_to_40_characters(delivery):
    nameless = delivery.put("/rider", headers=as_user(10, "rider"), json={"online": True, "lat": 24.8, "lng": 67.0})
    assert nameless.get_json()["name"] == "Rider 10"
    long = delivery.put("/rider", headers=as_user(11, "rider"), json={"online": True, "name": "N" * 80, "lat": 24.8, "lng": 67.0})
    assert len(long.get_json()["name"]) == 40


def test_distance_formula_is_sensible():
    assert delivery_service.distance_km(*CLIFTON, *CLIFTON) == 0
    assert 3 < delivery_service.distance_km(*CLIFTON, *DHA) < 5
    assert delivery_service.distance_km(*CLIFTON, *GULSHAN) == pytest.approx(delivery_service.distance_km(*GULSHAN, *CLIFTON))


# ================================================================ Alerts (Lambda and the pretend SNS)

def update(event="delivered", event_id="order-1-delivered", user_id=3, order_id=1, **details):
    return {"event_id": event_id, "event": event, "user_id": user_id, "order_id": order_id, "details": details}


def lambda_event(*updates):
    """What SQS hands to a real Lambda function."""
    return {"Records": [{"body": json.dumps(u)} for u in updates]}


def test_the_lambda_function_writes_the_alert_on_all_three_channels(folder):
    result = alert_lambda.handler(lambda_event(update("rider_assigned", "order-1-rider_assigned", rider="Ali", restaurant="Burger Lab", km="3.7")), folder=folder)
    assert result == {"processed": 1}
    with db("alerts", folder) as conn:
        rows = conn.execute("SELECT channel, text FROM outbox ORDER BY id").fetchall()
    assert [r["channel"] for r in rows] == ["push", "sms", "email"]
    assert rows[0]["text"] == "Order #1: Ali is on the way to Burger Lab (3.7 km from the restaurant)."


def test_the_same_event_twice_buzzes_the_phone_once(folder):
    for _ in range(2):
        alert_lambda.handler(lambda_event(update()), folder=folder)
    with db("alerts", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 3


def test_one_event_can_carry_several_records(folder):
    alert_lambda.handler(lambda_event(update(event_id="a"), update(event_id="b")), folder=folder)
    with db("alerts", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 6


def test_the_lambda_reads_the_updates_queue(folder):
    sqs.Queue(sqs.UPDATES, folder).send(update("payment_received", "order-1-payment_received", amount=550))
    assert alert_lambda.process_once(folder) == 1
    inbox = alert_lambda.create_app(folder).test_client().get("/inbox", headers=as_user(3)).get_json()["alerts"]
    assert inbox[0]["text"] == "Order #1: we received Rs 550. Now looking for a rider." and inbox[0]["event"] == "payment_received"


def test_a_message_the_lambda_cannot_understand_ends_up_in_the_dead_letter_queue(folder):
    sqs.Queue(sqs.UPDATES, folder).send({"event_id": "x", "event": "teleported", "user_id": 1, "order_id": 1})
    sqs.Queue(sqs.UPDATES, folder).send(update(event_id="good"))
    for _ in range(8):
        alert_lambda.process_once(folder)
    assert len(drain(folder, sqs.DEAD_LETTER)) == 1
    with db("alerts", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE event_id = 'good'").fetchone()[0] == 3   # the good one still went out


def test_the_inbox_shows_only_my_alerts_newest_first(folder):
    alert_lambda.handler(lambda_event(update(event_id="a", user_id=3, order_id=1), update(event_id="b", user_id=4, order_id=2),
                                      update("payment_declined", "c", user_id=3, order_id=5)), folder=folder)
    client = alert_lambda.create_app(folder).test_client()
    mine = client.get("/inbox", headers=as_user(3)).get_json()["alerts"]
    assert [a["order_id"] for a in mine] == [5, 1] and all(a["channel"] == "push" for a in mine)
    assert len(client.get("/inbox?channel=all", headers=as_user(3)).get_json()["alerts"]) == 6
    assert len(client.get("/inbox?channel=sms", headers=as_user(3)).get_json()["alerts"]) == 2
    assert len(client.get("/inbox?limit=1", headers=as_user(3)).get_json()["alerts"]) == 1
    assert client.get("/inbox?channel=pigeon", headers=as_user(3)).status_code == 400
    assert client.get("/inbox?limit=lots", headers=as_user(3)).status_code == 400
    assert client.get("/inbox").status_code == 401


def test_an_address_with_curly_braces_cannot_break_the_wording(folder):
    alert_lambda.handler(lambda_event(update("new_job", "j", restaurant="Burger {x}", address="{order_id} {0} {__class__}")), folder=folder)
    with db("alerts", folder) as conn:
        text = conn.execute("SELECT text FROM outbox WHERE channel = 'push'").fetchone()[0]
    assert text == "New delivery: order #1 from Burger {x} to {order_id} {0} {__class__}."


# ================================================================ Reports

def test_the_report_cube_counts_only_paid_orders_and_rolls_up(folder):
    client = order_service.create_app(folder).test_client()
    payment_service.create_app(folder)  # creates the payment tables
    place, ids = burger_lab(client)
    for card in ("tok_visa", "tok_visa", "tok_decline"):
        client.post("/orders", headers=as_user(1), json={"restaurant_id": place["id"], "address": "x", "card": card,
                                                         "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]})
    payment_service.process_once(folder)
    cube = reports.build_cube(folder)
    assert cube[("Burger Lab", "ALL")] == (2, 1100)
    assert cube[("ALL", "ALL")] == (2, 1100)
    assert [k for k in cube if k[0] == "Roll Express"] == []
    day = next(k[1] for k in cube if k[1] != "ALL")
    assert cube[("ALL", day)] == (2, 1100) and cube[("Burger Lab", day)] == (2, 1100)
    with db("reports", folder) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sales_cube").fetchone()[0] == 4


def test_the_report_script_explains_when_there_is_nothing_yet(folder, monkeypatch, capsys):
    monkeypatch.setenv("FOODCAT_DATA_DIR", folder)
    reports.main()
    assert "nothing to report yet" in capsys.readouterr().out.lower()


def test_the_report_script_prints_a_table(folder, monkeypatch, capsys):
    monkeypatch.setenv("FOODCAT_DATA_DIR", folder)
    client = order_service.create_app(folder).test_client()
    payment_service.create_app(folder)
    place, ids = burger_lab(client)
    client.post("/orders", headers=as_user(1), json={"restaurant_id": place["id"], "address": "x", "card": "tok_visa",
                                                     "items": [{"item_id": ids["Zinger Burger"], "qty": 1}]})
    payment_service.process_once(folder)
    reports.main()
    out = capsys.readouterr().out
    assert "Burger Lab" in out and "550" in out
