"""Order Service: menus and orders. It owns the Order DB (orders.db).

Browsing is served from the cache. Placing an order saves it, answers straight away
with the order number, and drops a message on the 'new orders' queue. It never calls
the Payment Service directly and never waits for it.
"""
import json
import time

from flask import jsonify, request

from . import sqs
from .cache import CacheDown, TTLCache
from .common import current_user, db, error, log, make_service, run, transaction

# Seed data (fictional restaurants). lat/lng lets the Delivery Service find the nearest rider.
# The cuisine is what the web page uses for its filter buttons.
RESTAURANTS = [
    {"id": 1, "name": "Biryani Junction", "cuisine": "Biryani", "area": "Clifton", "lat": 24.8138, "lng": 67.0300,
     "items": [("Chicken Biryani", 450), ("Beef Pulao", 420), ("Raita", 80), ("Kheer", 200)]},
    {"id": 2, "name": "Roll Express", "cuisine": "Rolls", "area": "Gulshan", "lat": 24.9215, "lng": 67.0920,
     "items": [("Seekh Kabab Roll", 320), ("Chicken Tikka Roll", 350), ("Fries", 180), ("Lassi", 150)]},
    {"id": 3, "name": "Burger Lab", "cuisine": "Burgers", "area": "DHA", "lat": 24.8010, "lng": 67.0640,
     "items": [("Zinger Burger", 550), ("Beef Smash Burger", 690), ("Onion Rings", 220), ("Cold Coffee", 280)]},
]


def _seed(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS restaurants (id INTEGER PRIMARY KEY, name TEXT, cuisine TEXT, area TEXT, lat REAL, lng REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS menu_items (id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER, name TEXT, price INTEGER)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               user_id INTEGER NOT NULL,
               restaurant_id INTEGER NOT NULL,
               items TEXT NOT NULL,
               total INTEGER NOT NULL,
               address TEXT NOT NULL,
               status TEXT NOT NULL,
               created REAL NOT NULL)"""
    )
    # Both order instances share this database and may start in the same moment. BEGIN IMMEDIATE makes the
    # second one wait, and then it sees that the menu is already there instead of adding it a second time.
    with transaction(conn):
        if "cuisine" not in [column["name"] for column in conn.execute("PRAGMA table_info(restaurants)")]:
            # A database made by an earlier version of Food Cat has no cuisine yet. Add it instead of failing.
            conn.execute("ALTER TABLE restaurants ADD COLUMN cuisine TEXT")
            for r in RESTAURANTS:
                conn.execute("UPDATE restaurants SET cuisine = ? WHERE id = ?", (r["cuisine"], r["id"]))
        if conn.execute("SELECT COUNT(*) FROM restaurants").fetchone()[0] == 0:
            for r in RESTAURANTS:
                conn.execute("INSERT INTO restaurants (id, name, cuisine, area, lat, lng) VALUES (?, ?, ?, ?, ?, ?)",
                             (r["id"], r["name"], r["cuisine"], r["area"], r["lat"], r["lng"]))
                for name, price in r["items"]:
                    conn.execute("INSERT INTO menu_items (restaurant_id, name, price) VALUES (?, ?, ?)", (r["id"], name, price))


def create_app(folder=None, cache=None, instance=None):
    app = make_service("order-service", instance)
    cache = cache or TTLCache(ttl=30)
    app.cache = cache  # tests and the demo switches reach it through here
    new_orders = sqs.Queue(sqs.NEW_ORDERS, folder)

    with db("orders", folder) as conn:
        _seed(conn)

    def load_menu():
        """Read the menu from the database (the slow path)."""
        with db("orders", folder) as conn:
            restaurants = [dict(r) for r in conn.execute("SELECT * FROM restaurants ORDER BY id")]
            for r in restaurants:
                rows = conn.execute("SELECT id, name, price FROM menu_items WHERE restaurant_id = ? ORDER BY id", (r["id"],))
                r["items"] = [dict(i) for i in rows]
        return restaurants

    @app.get("/menu")
    def menu():
        try:
            restaurants = cache.get("menu")
            if restaurants is not None:
                return jsonify(source="cache", restaurants=restaurants)
            restaurants = load_menu()
            cache.set("menu", restaurants)
            return jsonify(source="database", restaurants=restaurants)
        except CacheDown:
            # If the cache dies, we simply read the database instead. Slower, but nobody notices.
            log("order", "cache is down, reading the menu from the database")
            return jsonify(source="database (cache is down)", restaurants=load_menu())

    @app.post("/orders")
    def place_order():
        user = current_user()
        if not user:
            return error("Please log in first.", 401)
        if user["role"] != "customer":
            return error("Only customers can place orders.", 403)
        data = request.get_json(silent=True) or {}
        address = str(data.get("address", "")).strip()
        if not address or len(address) > 200:
            return error("Please give a delivery address (up to 200 characters).", 400)
        wanted = data.get("items")
        if not isinstance(wanted, list) or not wanted:
            return error("Please choose at least one item.", 400)

        with db("orders", folder) as conn:
            restaurant = conn.execute("SELECT * FROM restaurants WHERE id = ?", (data.get("restaurant_id"),)).fetchone()
            if restaurant is None:
                return error("Unknown restaurant.", 400)
            lines, total = [], 0
            for entry in wanted:
                try:
                    item_id, qty = int(entry["item_id"]), int(entry["qty"])
                except (KeyError, TypeError, ValueError):
                    return error("Each item needs an item_id and a qty.", 400)
                row = conn.execute("SELECT id, name, price FROM menu_items WHERE id = ? AND restaurant_id = ?",
                                   (item_id, restaurant["id"])).fetchone()
                if row is None or not 1 <= qty <= 20:
                    return error("Unknown item, or a quantity outside 1 to 20.", 400)
                lines.append({"item_id": row["id"], "name": row["name"], "qty": qty, "price": row["price"]})
                total += row["price"] * qty  # prices always come from our database, never from the client
            cur = conn.execute(
                "INSERT INTO orders (user_id, restaurant_id, items, total, address, status, created) VALUES (?, ?, ?, ?, ?, 'PLACED', ?)",
                (user["id"], restaurant["id"], json.dumps(lines), total, address, time.time()),
            )
            order_id = cur.lastrowid

        message = {
            "order_id": order_id, "user_id": user["id"], "amount": total, "address": address,
            "restaurant_id": restaurant["id"], "restaurant_name": restaurant["name"],
            "restaurant_lat": restaurant["lat"], "restaurant_lng": restaurant["lng"],
            "card_token": str(data.get("card") or "tok_visa")[:40],  # a token, never a real card number
        }
        try:
            new_orders.send(message)
        except Exception as exc:
            log("order", "could not queue order %s: %r" % (order_id, exc))
            with db("orders", folder) as conn:
                conn.execute("UPDATE orders SET status = 'FAILED' WHERE id = ?", (order_id,))
            return error("We couldn't queue your order. Nothing was charged, please try again.", 503)
        log("order", "order %s saved (Rs %s) and sent to the new-orders queue" % (order_id, total))
        return jsonify(order_id=order_id, status="PLACED", total=total, restaurant=restaurant["name"]), 201

    def _row_to_order(row):
        order = dict(row)
        order["items"] = json.loads(order["items"])
        return order

    @app.get("/orders")
    def my_orders():
        user = current_user()
        if not user:
            return error("Please log in first.", 401)
        with db("orders", folder) as conn:
            rows = conn.execute(
                "SELECT o.*, r.name AS restaurant FROM orders o JOIN restaurants r ON r.id = o.restaurant_id "
                "WHERE o.user_id = ? ORDER BY o.id DESC LIMIT 20", (user["id"],)).fetchall()
        return jsonify(orders=[_row_to_order(r) for r in rows])

    @app.get("/orders/<int:order_id>")
    def one_order(order_id):
        user = current_user()
        if not user:
            return error("Please log in first.", 401)
        with db("orders", folder) as conn:
            row = conn.execute(
                "SELECT o.*, r.name AS restaurant FROM orders o JOIN restaurants r ON r.id = o.restaurant_id "
                "WHERE o.id = ? AND o.user_id = ?", (order_id, user["id"])).fetchone()
        if row is None:
            return error("No such order.", 404)  # also the answer for somebody else's order
        return jsonify(_row_to_order(row))

    # Demo switches (not reachable through the gateway): rehearse a cache failure.
    @app.post("/admin/cache/<state>")
    def cache_switch(state):
        if state not in ("down", "up"):
            return error("Use /admin/cache/down or /admin/cache/up", 400)
        cache.down = state == "down"
        cache.clear()
        log("order", "cache switched %s" % state)
        return jsonify(cache_down=cache.down)

    return app


def main():
    run(create_app(), 5002)


if __name__ == "__main__":
    main()
