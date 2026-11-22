"""Whole journeys through the real system: gateway, services, queues, Lambda.

The three background workers are run by hand with settle(), so every test is quick and
gives the same result every time.
"""
from foodcat import sqs

from conftest import CLIFTON, GULSHAN, drain, settle


def events(stack, token):
    """What this person's phone received, oldest first."""
    return [a["event"] for a in reversed(stack.alerts(token))]


def test_a_full_order_from_click_to_doorstep(stack):
    customer, customer_id = stack.join("Ayesha Khan")
    ali, _ = stack.join("Ali", "rider")
    sara, _ = stack.join("Sara", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)       # close to Burger Lab
    stack.rider_online(sara, "Sara", GULSHAN)     # far from Burger Lab

    placed = stack.order(customer, qty=2, address="House 12, Street 5, DHA")
    assert placed.status_code == 201 and placed.get_json()["total"] == 1100
    order_id = placed.get_json()["order_id"]

    # The order is saved and queued. Nothing else has happened yet: the answer did not wait for it.
    assert stack.alerts(customer) == []
    assert sqs.Queue(sqs.NEW_ORDERS, stack.folder).size() == 1

    settle(stack.folder)
    assert events(stack, customer) == ["payment_received", "rider_assigned"]
    job_alert = stack.alerts(ali)[0]
    assert job_alert["event"] == "new_job" and "House 12, Street 5, DHA" in job_alert["text"]
    assert stack.alerts(sara) == []               # Ali was nearer, so Sara heard nothing

    assert stack.call("POST", "/delivery/jobs/%d/pickup" % order_id, ali).status_code == 200
    settle(stack.folder)
    assert stack.call("POST", "/delivery/jobs/%d/deliver" % order_id, ali).status_code == 200
    settle(stack.folder)
    assert events(stack, customer) == ["payment_received", "rider_assigned", "picked_up", "delivered"]

    listed = stack.call("GET", "/order/orders", customer).get_json()["orders"]
    assert [o["id"] for o in listed] == [order_id] and listed[0]["restaurant"] == "Burger Lab"
    for name in (sqs.NEW_ORDERS, sqs.PAID_ORDERS, sqs.UPDATES, sqs.DEAD_LETTER):
        assert sqs.Queue(name, stack.folder).size() == 0, name
    assert customer_id == stack.call("GET", "/user/me", customer).get_json()["id"]


def test_a_declined_card_cancels_the_order_and_no_rider_is_sent(stack):
    customer, _ = stack.join("Ayesha Khan")
    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    assert stack.order(customer, card="tok_decline").status_code == 201
    settle(stack.folder)
    assert events(stack, customer) == ["payment_declined"]
    assert "declined" in stack.alerts(customer)[0]["text"]
    assert stack.alerts(ali) == []
    assert stack.call("GET", "/delivery/jobs", ali).get_json()["jobs"] == []
    assert sqs.Queue(sqs.PAID_ORDERS, stack.folder).size() == 0


def test_an_order_waits_for_a_rider_instead_of_being_lost(stack):
    customer, _ = stack.join("Ayesha Khan")
    stack.order(customer)
    settle(stack.folder)
    assert events(stack, customer) == ["payment_received"]           # paid, but nobody to deliver yet
    assert sqs.Queue(sqs.PAID_ORDERS, stack.folder).size() == 1

    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    settle(stack.folder)
    assert events(stack, customer) == ["payment_received", "rider_assigned"]
    assert sqs.Queue(sqs.PAID_ORDERS, stack.folder).size() == 0


def test_a_busy_rider_gets_the_next_order_only_after_delivering(stack):
    ayesha, _ = stack.join("Ayesha Khan")
    zainab, _ = stack.join("Zainab Shah")
    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    first = stack.order(ayesha).get_json()["order_id"]
    second = stack.order(zainab).get_json()["order_id"]
    settle(stack.folder)
    assert events(stack, ayesha) == ["payment_received", "rider_assigned"]
    assert events(stack, zainab) == ["payment_received"]             # Ali is busy with the first order
    assert [j["order_id"] for j in stack.call("GET", "/delivery/jobs", ali).get_json()["jobs"]] == [first]

    stack.call("POST", "/delivery/jobs/%d/pickup" % first, ali)
    stack.call("POST", "/delivery/jobs/%d/deliver" % first, ali)
    settle(stack.folder)
    assert events(stack, zainab) == ["payment_received", "rider_assigned"]
    assert {j["order_id"] for j in stack.call("GET", "/delivery/jobs", ali).get_json()["jobs"]} == {first, second}


def test_people_never_see_each_others_orders_or_alerts(stack):
    ayesha, _ = stack.join("Ayesha Khan")
    zainab, _ = stack.join("Zainab Shah")
    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    order_id = stack.order(ayesha).get_json()["order_id"]
    settle(stack.folder)
    assert stack.call("GET", "/order/orders", zainab).get_json()["orders"] == []
    assert stack.call("GET", "/order/orders/%d" % order_id, zainab).status_code == 404
    assert stack.alerts(zainab) == []
    assert stack.call("GET", "/order/orders/%d" % order_id, ayesha).status_code == 200


def test_a_dead_card_company_ends_in_the_dead_letter_queue_and_the_monitor_shows_it(stack):
    customer, _ = stack.join("Ayesha Khan")
    stack.order(customer, card="tok_error")
    for _ in range(8):
        settle(stack.folder)
    assert events(stack, customer) == []                              # nothing was charged, nothing was promised
    report = stack.client.get("/api/monitor").get_json()
    assert report["queues"]["dead-letter"] == 1 and report["queues"]["new-orders"] == 0
    parked = drain(stack.folder, sqs.DEAD_LETTER)[0]
    assert parked["from"] == sqs.NEW_ORDERS and parked["body"]["card_token"] == "tok_error"


def test_the_whole_system_keeps_working_while_one_order_instance_is_down(stack):
    customer, _ = stack.join("Ayesha Khan")
    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    stack.stop("order-1")
    assert stack.order(customer).status_code == 201
    settle(stack.folder)
    assert events(stack, customer) == ["payment_received", "rider_assigned"]


def test_the_same_alert_is_not_sent_twice_even_if_a_message_is_repeated(stack):
    customer, _ = stack.join("Ayesha Khan")
    ali, _ = stack.join("Ali", "rider")
    stack.rider_online(ali, "Ali", CLIFTON)
    stack.order(customer)
    # Somebody (or SQS) puts a copy of the new-order message back on the queue.
    queue = sqs.Queue(sqs.NEW_ORDERS, stack.folder)
    copy = queue.receive()[0].body
    queue.send(copy)
    queue.send(copy)
    settle(stack.folder)
    assert events(stack, customer) == ["payment_received", "rider_assigned"]
    assert len(stack.call("GET", "/delivery/jobs", ali).get_json()["jobs"]) == 1
    assert len(stack.alerts(ali, channel="all")) == 3                 # one job alert, on push, sms and email
