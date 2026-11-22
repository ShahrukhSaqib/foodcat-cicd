"""Alerts: the Lambda that sends messages to users, and a pretend SNS.

On AWS this part would be:
  SQS 'updates' queue → Lambda function → SNS → push, SMS and email on the user's phone

Here it is three small pieces in one file:
  handler()       the Lambda function. It has the same shape as a real one: handler(event).
                  'event' looks like what SQS hands to Lambda: {"Records": [{"body": "<json>"}]}
  publish()       the pretend SNS. It writes one row per channel (push, sms, email) into alerts.db.
  the web API     a stand-in for the user's phone. The web page asks it for the user's push alerts.
                  (A real phone would get them pushed. The browser has to ask.)

Services never write wording. They post facts ('rider_assigned') and the wording lives
here, in TEMPLATES, so every message sounds the same.
"""
import json
import time

from flask import jsonify, request

from . import sqs
from .common import current_user, db, error, log, loop_forever, make_service, run

CHANNELS = ("push", "sms", "email")

TEMPLATES = {
    "payment_received": "Order #{order_id}: we received Rs {amount}. Now looking for a rider.",
    "payment_declined": "Order #{order_id}: your card was declined, so the order is cancelled. Nothing was charged.",
    "rider_assigned": "Order #{order_id}: {rider} is on the way to {restaurant} ({km} km from the restaurant).",
    "new_job": "New delivery: order #{order_id} from {restaurant} to {address}.",
    "picked_up": "Order #{order_id}: {rider} has picked up your food.",
    "delivered": "Order #{order_id}: delivered. Enjoy your meal!",
}


def _setup(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               event_id TEXT NOT NULL,
               event TEXT NOT NULL,
               channel TEXT NOT NULL,
               user_id INTEGER NOT NULL,
               order_id INTEGER NOT NULL,
               text TEXT NOT NULL,
               created REAL NOT NULL,
               UNIQUE (event_id, channel))"""
    )


def publish(conn, user_id, order_id, event_id, event, text):
    """The pretend SNS. The same event id is stored only once, so a message that arrives
    twice from the queue does not buzz the phone twice."""
    sent = 0
    for channel in CHANNELS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO outbox (event_id, event, channel, user_id, order_id, text, created) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_id, event, channel, user_id, order_id, text, time.time()),
        )
        sent += cur.rowcount
    if sent:  # the log names the event, not the text, because the text can hold a home address
        log("sns", "push, sms and email sent to user %s (%s)" % (user_id, event_id))


def handler(event, context=None, folder=None):
    """The Lambda function. Raises on a bad message, so SQS can retry it."""
    count = 0
    with db("alerts", folder) as conn:
        _setup(conn)
        for record in event["Records"]:
            update = json.loads(record["body"])
            values = dict(update.get("details") or {}, order_id=update["order_id"])
            text = TEMPLATES[update["event"]].format_map(values)
            publish(conn, update["user_id"], update["order_id"], update["event_id"], update["event"], text)
            count += 1
    return {"processed": count}


def process_once(folder=None):
    """What AWS does for you (the event source mapping): read the queue and call the function."""
    queue = sqs.Queue(sqs.UPDATES, folder)

    def call_lambda(body):
        handler({"Records": [{"body": json.dumps(body)}]}, folder=folder)

    return sqs.consume(queue, call_lambda, service="lambda")


def create_app(folder=None, instance=None):
    app = make_service("alerts", instance)
    with db("alerts", folder) as conn:
        _setup(conn)

    @app.get("/inbox")
    def inbox():
        user = current_user()
        if not user:
            return error("Please log in first.", 401)
        channel = request.args.get("channel", "push")
        if channel not in CHANNELS + ("all",):
            return error("Channel must be push, sms, email or all.", 400)
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 100))
        except ValueError:
            return error("limit must be a number.", 400)
        sql = "SELECT id, order_id, event, channel, text, created FROM outbox WHERE user_id = ?"
        params = [user["id"]]
        if channel != "all":
            sql += " AND channel = ?"
            params.append(channel)
        with db("alerts", folder) as conn:
            rows = conn.execute(sql + " ORDER BY id DESC LIMIT ?", params + [limit]).fetchall()
        return jsonify(alerts=[dict(r) for r in rows])

    return app


def main():
    run(create_app(), 5005, worker=lambda: loop_forever(process_once))


if __name__ == "__main__":
    main()
