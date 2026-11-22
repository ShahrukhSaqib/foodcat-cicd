"""A guided demo of Food Cat. Start the system first, then run this in a second window:

    python run_local.py
    python demo.py

It plays one customer and two riders through the whole architecture and prints what
happens at each step. It only talks to the gateway (the front door), the same way the
web page does. Add --break-cache to also rehearse a cache failure (this one talks to
the order services directly, so it only works with run_local.py).
"""
import argparse
import sys
import time
import uuid

import requests

CLIFTON = (24.8138, 67.0300)  # near Biryani Junction and quite close to Burger Lab
GULSHAN = (24.9215, 67.0920)  # far from Burger Lab
ORDER_SERVICE_PORTS = (5002, 5012)  # where run_local.py starts the two order services (only --break-cache needs them)
POLL_SECONDS = 0.4  # how often the demo looks again while the background work is going on
JOB_ALERT_SECONDS = 5  # the customer's alert and Ali's job alert are made together, so Ali's follows within moments


class DemoFailed(Exception):
    pass


class Api:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.session = requests.Session()
        self.session.trust_env = False  # talk to our own computer directly, even if a proxy is set for the internet

    def call(self, method, path, token=None, expect=None, **body):
        headers = {"Authorization": "Bearer " + token} if token else {}
        reply = self.session.request(method, self.base + path, json=body or None, headers=headers, timeout=15)
        if expect and reply.status_code != expect:
            raise DemoFailed("%s %s answered %s: %s" % (method, path, reply.status_code, reply.text[:200]))
        return reply


def say(text=""):
    print(text, flush=True)


def wait_for(what, check, seconds=25):
    """Keep asking until check() gives something, because the work happens in the background."""
    end = time.time() + seconds
    while time.time() < end:
        found = check()
        if found:
            return found
        time.sleep(POLL_SECONDS)
    raise DemoFailed("Waited %d seconds but this never happened: %s" % (seconds, what))


def inbox(api, token):
    return api.call("GET", "/api/alerts/inbox", token, expect=200).json()["alerts"]


def has_alert(api, token, order_id, words):
    for alert in inbox(api, token):
        if alert["order_id"] == order_id and words in alert["text"]:
            return alert
    return None


def go_offline(api, people):
    """Leave the system as we found it. The demo riders go offline, so the next run starts clean
    (otherwise last time's Ali would still be online, free and just as close, and he would get the job)."""
    for key in ("ali", "sara"):
        if key in people:
            api.call("PUT", "/api/delivery/rider", people[key], online=False)


def run(api, break_cache):
    people = {}
    try:
        play(api, break_cache, people)
    finally:
        go_offline(api, people)


def play(api, break_cache, people):
    stamp = uuid.uuid4().hex[:8]  # makes every run sign up with new email addresses
    password = "demo-pass-" + stamp[-4:]

    say("Step 1. Sign up a customer and two riders (User Service)")
    for key, name, role in (("customer", "Demo Customer", "customer"), ("ali", "Ali", "rider"), ("sara", "Sara", "rider")):
        email = "%s.%s@example.com" % (key, stamp)
        api.call("POST", "/api/user/signup", expect=201, name=name, email=email, password=password, role=role)
        token = api.call("POST", "/api/user/login", expect=200, email=email, password=password).json()["token"]
        people[key] = token
        say("   %s signed up and logged in as a %s" % (name, role))

    say()
    say("Step 2. Riders go online with their location (Delivery DB, the NoSQL part)")
    api.call("PUT", "/api/delivery/rider", people["ali"], expect=200, online=True, name="Ali", lat=CLIFTON[0], lng=CLIFTON[1], area="Clifton")
    api.call("PUT", "/api/delivery/rider", people["sara"], expect=200, online=True, name="Sara", lat=GULSHAN[0], lng=GULSHAN[1], area="Gulshan")
    say("   Ali is in Clifton and Sara is in Gulshan")

    say()
    say("Step 3. The customer opens the menu four times (gateway load balancer and cache)")
    menu = None
    for turn in range(1, 5):
        reply = api.call("GET", "/api/order/menu", expect=200)
        menu = reply.json()
        say("   call %d was answered by %s and read from the %s" % (turn, reply.headers.get("X-Served-By"), menu["source"]))
    say("   The gateway alternates between the two order instances. Each instance has its own cache,")
    say("   so each reads the database once and then answers from the cache.")

    if break_cache:
        say()
        say("   Rehearsal: the cache dies on both order instances (called directly, not through the gateway)")
        try:
            for port in ORDER_SERVICE_PORTS:
                api.session.post("http://127.0.0.1:%d/admin/cache/down" % port, timeout=5).raise_for_status()
        except requests.ConnectionError:
            raise DemoFailed("--break-cache talks to the two order services directly, so it only works with the local run "
                             "(python run_local.py), not with Docker") from None
        reply = api.call("GET", "/api/order/menu", expect=200)
        say("   the menu still works: read from the %s" % reply.json()["source"])
        for port in ORDER_SERVICE_PORTS:
            api.session.post("http://127.0.0.1:%d/admin/cache/up" % port, timeout=5).raise_for_status()
        say("   cache repaired")

    restaurant = next(r for r in menu["restaurants"] if r["name"] == "Burger Lab")
    zinger = next(i for i in restaurant["items"] if i["name"] == "Zinger Burger")
    coffee = next(i for i in restaurant["items"] if i["name"] == "Cold Coffee")

    say()
    say("Step 4. The customer orders from Burger Lab (DHA). The Order Service answers at once.")
    started = time.time()
    order = api.call("POST", "/api/order/orders", people["customer"], expect=201, restaurant_id=restaurant["id"],
                     items=[{"item_id": zinger["id"], "qty": 2}, {"item_id": coffee["id"], "qty": 1}],
                     address="House 12, Street 5, Khayaban-e-Shahbaz, DHA", card="tok_visa").json()
    say("   order %s saved for Rs %s in %d ms. Payment and delivery are still to come." % (
        order["order_id"], order["total"], (time.time() - started) * 1000))
    order_id = order["order_id"]

    say()
    say("Step 5. In the background: pay (queue), find a rider (queue), alert (queue and Lambda)")
    wait_for("the payment alert", lambda: has_alert(api, people["customer"], order_id, "we received"))
    say("   payment received")
    assigned = wait_for("a rider for the order", lambda: has_alert(api, people["customer"], order_id, "is on the way"))
    say("   " + assigned["text"])
    try:
        ali_job = wait_for("the job alert for Ali", lambda: has_alert(api, people["ali"], order_id, "New delivery"),
                           seconds=JOB_ALERT_SECONDS)
    except DemoFailed:
        raise DemoFailed("the job went to a rider who was already online before the demo started. The customer was told: %s "
                         "The demo needs a system where only Ali and Sara are online, so stop the system and start it "
                         "again with: python run_local.py --fresh" % assigned["text"]) from None
    say("   Ali's phone: " + ali_job["text"])
    if has_alert(api, people["sara"], order_id, "New delivery"):
        raise DemoFailed("Sara was farther away but still got the job")
    say("   Sara got nothing because Ali is the nearest rider")

    say()
    say("Step 6. Ali picks up and delivers")
    api.call("POST", "/api/delivery/jobs/%s/pickup" % order_id, people["ali"], expect=200)
    wait_for("the pickup alert", lambda: has_alert(api, people["customer"], order_id, "picked up"))
    api.call("POST", "/api/delivery/jobs/%s/deliver" % order_id, people["ali"], expect=200)
    wait_for("the delivery alert", lambda: has_alert(api, people["customer"], order_id, "delivered"))
    say("   delivered")

    say()
    say("Step 7. What the customer's phone received, oldest first")
    for alert in reversed(inbox(api, people["customer"])):
        say("   " + alert["text"])

    say()
    say("Step 8. A second order with a card that gets declined")
    bad = api.call("POST", "/api/order/orders", people["customer"], expect=201, restaurant_id=restaurant["id"],
                   items=[{"item_id": zinger["id"], "qty": 1}], address="Flat 3, Block B, Clifton", card="tok_decline").json()
    wait_for("the decline alert", lambda: has_alert(api, people["customer"], bad["order_id"], "declined"))
    say("   the customer was told the card was declined")
    if has_alert(api, people["ali"], bad["order_id"], "New delivery"):
        raise DemoFailed("A rider was sent for an order that was never paid")
    say("   and no rider was sent, because only paid orders reach the Delivery Service")

    say()
    say("Step 9. The monitoring report (gateway /api/monitor)")
    report = api.call("GET", "/api/monitor", expect=200).json()
    for name, instances in report["services"].items():
        say("   %-9s %s" % (name, ", ".join("%s %s" % (i["instance"], i["status"]) for i in instances)))
    say("   queues    " + ", ".join("%s: %s" % (name, size) for name, size in report["queues"].items()))
    say()
    say("Demo finished. Everything worked.")


def main():
    parser = argparse.ArgumentParser(description="Play one order through Food Cat.")
    parser.add_argument("--base", default="http://127.0.0.1:8000", help="address of the gateway")
    parser.add_argument("--break-cache", action="store_true", help="also rehearse a cache failure (local run only)")
    args = parser.parse_args()
    try:
        run(Api(args.base), args.break_cache)
    except requests.ConnectionError:
        sys.exit("I could not reach the gateway. Start the system first with: python run_local.py")
    except DemoFailed as problem:
        sys.exit("Demo failed: %s" % problem)


if __name__ == "__main__":
    main()
