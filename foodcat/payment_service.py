"""Payment Service: takes the money. It owns the Payment DB (payments.db).

It has no public door. The gateway has no route to it, so nobody outside can call it.
It only listens to the 'new orders' queue, charges the card and posts the result:
  paid      → 'paid orders' queue (the Delivery Service picks it up) and a message on 'updates'
  declined  → a message on 'updates' only (the customer is told and no rider is used)

The card company is faked. A card token decides what happens:
  tok_decline  the card is refused
  tok_error    the card company does not answer (we retry, and give up after 5 tries)
  anything else  the payment works
"""
import time
import uuid

from . import sqs
from .common import db, log, loop_forever, make_service, run, transaction


class ProviderDown(Exception):
    """The card company did not answer. The message goes back on the queue and we try again."""


def _setup(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS payments (
               order_id INTEGER PRIMARY KEY,
               user_id INTEGER NOT NULL,
               amount INTEGER NOT NULL,
               status TEXT NOT NULL,
               reference TEXT NOT NULL,
               created REAL NOT NULL)"""
    )
    # This table pretends to be the card company's own memory (see charge below).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS provider_charges (
               idem_key TEXT PRIMARY KEY,
               status TEXT NOT NULL,
               reference TEXT NOT NULL)"""
    )


def charge(conn, key, card_token, amount):
    """Pretend card company. Asking twice with the same key never charges twice, which is
    what real providers do with an idempotency key. It protects us when our service crashes
    right after the charge: the retry gets the old answer instead of charging again."""
    seen = conn.execute("SELECT status, reference FROM provider_charges WHERE idem_key = ?", (key,)).fetchone()
    if seen:
        return seen["status"], seen["reference"]
    if card_token == "tok_error":
        raise ProviderDown("the card company did not answer")
    status = "DECLINED" if card_token == "tok_decline" else "PAID"
    reference = "pay_" + uuid.uuid4().hex[:10]
    conn.execute("INSERT INTO provider_charges (idem_key, status, reference) VALUES (?, ?, ?)", (key, status, reference))
    return status, reference


def handle_new_order(order, folder=None):
    """Handle one message from the 'new orders' queue. Safe to run twice for the same order."""
    order_id = order["order_id"]
    paid_queue = sqs.Queue(sqs.PAID_ORDERS, folder)
    updates = sqs.Queue(sqs.UPDATES, folder)
    with db("payments", folder) as conn:
        _setup(conn)
        # The charge is saved at once (autocommit), like a real card company that never forgets it.
        status, reference = charge(conn, "order-%s" % order_id, order.get("card_token", ""), order["amount"])
        with transaction(conn):
            conn.execute(
                "INSERT OR IGNORE INTO payments (order_id, user_id, amount, status, reference, created) VALUES (?, ?, ?, ?, ?, ?)",
                (order_id, order["user_id"], order["amount"], status, reference, time.time()),
            )
            # The messages go out before COMMIT. If anything fails the whole step is retried, and the
            # fixed event ids mean nobody gets the same alert twice.
            if status == "PAID":
                paid_queue.send({  # the card token stays here: Delivery does not need it
                    "order_id": order_id, "user_id": order["user_id"], "address": order["address"],
                    "restaurant_name": order["restaurant_name"],
                    "restaurant_lat": order["restaurant_lat"], "restaurant_lng": order["restaurant_lng"],
                })
                updates.send({"event_id": "order-%s-paid" % order_id, "event": "payment_received",
                              "user_id": order["user_id"], "order_id": order_id,
                              "details": {"amount": order["amount"]}})
            else:
                updates.send({"event_id": "order-%s-declined" % order_id, "event": "payment_declined",
                              "user_id": order["user_id"], "order_id": order_id, "details": {}})
    log("payment", "order %s: %s" % (order_id, status.lower()))


def process_once(folder=None):
    """One round of the worker: take what is on the queue and handle it."""
    queue = sqs.Queue(sqs.NEW_ORDERS, folder)
    return sqs.consume(queue, lambda body: handle_new_order(body, folder), service="payment")


def create_app(folder=None, instance=None):
    app = make_service("payment-service", instance)
    with db("payments", folder) as conn:
        _setup(conn)
    return app  # only /health: this service is reached through the queue, never by HTTP


def main():
    run(create_app(), 5003, worker=lambda: loop_forever(process_once))


if __name__ == "__main__":
    main()
