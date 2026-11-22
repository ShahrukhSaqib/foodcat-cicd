"""Delivery Service: finds a rider. It owns the Delivery DB (delivery.db).

Two jobs:
  1. A worker listens to the 'paid orders' queue and gives each order to the nearest free rider.
     If nobody is free, the order simply waits in the queue and is tried again a little later.
  2. A small web API for the rider app: go online, send my location, see my jobs, mark them done.

The diagram says the Delivery DB is NoSQL (rider locations). The 'riders' table below is a
key plus a JSON document, which is how DynamoDB or MongoDB store data. SQLite is just the
engine under it, so nothing extra needs installing.
"""
import json
import math
import time

from flask import jsonify, request

from . import sqs
from .common import current_user, db, error, log, loop_forever, make_service, run, transaction


def _setup(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS riders (rider_id INTEGER PRIMARY KEY, doc TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
               order_id INTEGER PRIMARY KEY,
               customer_id INTEGER NOT NULL,
               rider_id INTEGER NOT NULL,
               restaurant TEXT NOT NULL,
               address TEXT NOT NULL,
               status TEXT NOT NULL,
               distance_km REAL NOT NULL,
               created REAL NOT NULL,
               updated REAL NOT NULL)"""
    )


def distance_km(lat1, lng1, lat2, lng2):
    """Straight line distance on the earth (the haversine formula). Good enough to pick the nearest rider."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def _update(order_id, user_id, event, **details):
    """The message for the 'updates' queue. The id is fixed per event, so a retry never sends it twice."""
    return {"event_id": "order-%s-%s" % (order_id, event), "event": event,
            "user_id": user_id, "order_id": order_id, "details": details}


def assign_rider(order, folder=None):
    """Handle one message from the 'paid orders' queue. Safe to run twice for the same order."""
    order_id = order["order_id"]
    updates = sqs.Queue(sqs.UPDATES, folder)
    with db("delivery", folder) as conn:
        _setup(conn)
        with transaction(conn):
            done = conn.execute("SELECT rider_id, distance_km FROM deliveries WHERE order_id = ?", (order_id,)).fetchone()
            if done:  # we already did this one (the message came twice), so just repeat the alerts
                rider_id, km = done["rider_id"], done["distance_km"]
                doc = json.loads(conn.execute("SELECT doc FROM riders WHERE rider_id = ?", (rider_id,)).fetchone()["doc"])
            else:
                best = None
                for row in conn.execute("SELECT rider_id, doc FROM riders ORDER BY rider_id"):
                    doc = json.loads(row["doc"])
                    if not doc.get("online") or doc.get("busy_with"):
                        continue
                    km = distance_km(doc["lat"], doc["lng"], order["restaurant_lat"], order["restaurant_lng"])
                    if best is None or km < best[0]:
                        best = (km, row["rider_id"], doc)
                if best is None:
                    raise sqs.Retry("no rider is free yet, so order %s waits in the queue" % order_id)
                km, rider_id, doc = best
                doc["busy_with"] = order_id
                now = time.time()
                conn.execute("UPDATE riders SET doc = ? WHERE rider_id = ?", (json.dumps(doc), rider_id))
                conn.execute(
                    "INSERT INTO deliveries (order_id, customer_id, rider_id, restaurant, address, status, distance_km, created, updated) "
                    "VALUES (?, ?, ?, ?, ?, 'ASSIGNED', ?, ?, ?)",
                    (order_id, order["user_id"], rider_id, order["restaurant_name"], order["address"], km, now, now),
                )
            updates.send(_update(order_id, order["user_id"], "rider_assigned",
                                 rider=doc["name"], restaurant=order["restaurant_name"], km="%.1f" % km))
            updates.send(_update(order_id, rider_id, "new_job",
                                 restaurant=order["restaurant_name"], address=order["address"]))
    log("delivery", "order %s given to %s (%.1f km from the restaurant)" % (order_id, doc["name"], km))


def process_once(folder=None):
    """One round of the worker: take what is on the queue and handle it."""
    queue = sqs.Queue(sqs.PAID_ORDERS, folder)
    return sqs.consume(queue, lambda body: assign_rider(body, folder), service="delivery")


def _number(value, low, high):
    """A float inside [low, high], or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def create_app(folder=None, instance=None):
    app = make_service("delivery-service", instance)
    with db("delivery", folder) as conn:
        _setup(conn)

    def rider_only():
        user = current_user()
        if not user:
            return None, error("Please log in first.", 401)
        if user["role"] != "rider":
            return None, error("Only riders can use this.", 403)
        return user, None

    @app.put("/rider")
    def set_status():
        user, problem = rider_only()
        if problem:
            return problem
        data = request.get_json(silent=True) or {}
        online = data.get("online")
        if not isinstance(online, bool):
            return error("Please say whether you are online (true or false).", 400)
        with db("delivery", folder) as conn:
            with transaction(conn):
                row = conn.execute("SELECT doc FROM riders WHERE rider_id = ?", (user["id"],)).fetchone()
                doc = json.loads(row["doc"]) if row else {"busy_with": None}
                if not online and doc.get("busy_with"):
                    return error("Please finish your current delivery before going offline.", 409)
                name = (str(data.get("name") or "").strip() or doc.get("name") or "Rider %s" % user["id"])[:40]
                if online:
                    lat = _number(data.get("lat"), -90, 90)
                    lng = _number(data.get("lng"), -180, 180)
                    if lat is None or lng is None:
                        return error("Going online needs your location (lat and lng).", 400)
                    doc.update(lat=lat, lng=lng, area=str(data.get("area") or "")[:40])
                doc.update(name=name, online=online, updated=time.time())
                conn.execute("INSERT INTO riders (rider_id, doc) VALUES (?, ?) "
                             "ON CONFLICT(rider_id) DO UPDATE SET doc = excluded.doc", (user["id"], json.dumps(doc)))
        log("delivery", "%s is now %s" % (name, "online" if online else "offline"))
        return jsonify(doc)

    @app.get("/rider")
    def get_status():
        user, problem = rider_only()
        if problem:
            return problem
        with db("delivery", folder) as conn:
            row = conn.execute("SELECT doc FROM riders WHERE rider_id = ?", (user["id"],)).fetchone()
        return jsonify(json.loads(row["doc"]) if row else {"online": False, "busy_with": None})

    @app.get("/jobs")
    def my_jobs():
        user, problem = rider_only()
        if problem:
            return problem
        with db("delivery", folder) as conn:
            rows = conn.execute("SELECT * FROM deliveries WHERE rider_id = ? ORDER BY created DESC LIMIT 20",
                                (user["id"],)).fetchall()
        return jsonify(jobs=[dict(r) for r in rows])

    def move_job(order_id, expected, new_status, event):
        user, problem = rider_only()
        if problem:
            return problem
        with db("delivery", folder) as conn:
            with transaction(conn):
                job = conn.execute("SELECT * FROM deliveries WHERE order_id = ? AND rider_id = ?",
                                   (order_id, user["id"])).fetchone()
                if job is None:
                    return error("No such job.", 404)
                if job["status"] != expected:
                    return error("This job is %s, so it can't go to %s." % (job["status"], new_status), 409)
                doc = json.loads(conn.execute("SELECT doc FROM riders WHERE rider_id = ?", (user["id"],)).fetchone()["doc"])
                sqs.Queue(sqs.UPDATES, folder).send(_update(order_id, job["customer_id"], event, rider=doc["name"]))
                conn.execute("UPDATE deliveries SET status = ?, updated = ? WHERE order_id = ?",
                             (new_status, time.time(), order_id))
                if new_status == "DELIVERED":  # the rider is free again for the next order
                    doc["busy_with"] = None
                    conn.execute("UPDATE riders SET doc = ? WHERE rider_id = ?", (json.dumps(doc), user["id"]))
        log("delivery", "order %s is %s" % (order_id, new_status))
        return jsonify(order_id=order_id, status=new_status)

    @app.post("/jobs/<int:order_id>/pickup")
    def pickup(order_id):
        return move_job(order_id, "ASSIGNED", "PICKED_UP", "picked_up")

    @app.post("/jobs/<int:order_id>/deliver")
    def deliver(order_id):
        return move_job(order_id, "PICKED_UP", "DELIVERED", "delivered")

    return app


def main():
    run(create_app(), 5004, worker=lambda: loop_forever(process_once))


if __name__ == "__main__":
    main()
