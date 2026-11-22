"""Reports: a tiny OLAP cube (the 'Reports' box in the diagram).

It is not part of the live ordering path, which is why it has no arrows in the drawing.
It is a batch job, the kind you would run every night. It reads the paid orders, adds them
up for every combination of restaurant and day (including 'ALL'), and saves the result in
reports.db. Managers can then ask 'sales per restaurant' or 'sales per day' without
touching the busy order database.

Run it with:  python -m foodcat.reports
"""
import sqlite3
import time

from .common import db, transaction


def build_cube(folder=None):
    """Return {(restaurant, day): (orders, revenue)} and save it to reports.db."""
    with db("payments", folder) as pay:
        paid = {r["order_id"]: r["amount"] for r in pay.execute("SELECT order_id, amount FROM payments WHERE status = 'PAID'")}
    with db("orders", folder) as orders:
        rows = orders.execute(
            "SELECT o.id, o.created, r.name FROM orders o JOIN restaurants r ON r.id = o.restaurant_id"
        ).fetchall()

    cube = {}
    for row in rows:
        if row["id"] not in paid:  # declined or unpaid orders are not sales
            continue
        day = time.strftime("%Y-%m-%d", time.localtime(row["created"]))
        # Every order counts once in each of four cells: the cube is these roll ups.
        for key in ((row["name"], day), (row["name"], "ALL"), ("ALL", day), ("ALL", "ALL")):
            cell = cube.setdefault(key, [0, 0])
            cell[0] += 1
            cell[1] += paid[row["id"]]

    with db("reports", folder) as out:
        out.execute(
            "CREATE TABLE IF NOT EXISTS sales_cube "
            "(restaurant TEXT, day TEXT, orders INTEGER, revenue INTEGER, PRIMARY KEY (restaurant, day))"
        )
        with transaction(out):
            out.execute("DELETE FROM sales_cube")
            out.executemany("INSERT INTO sales_cube VALUES (?, ?, ?, ?)",
                            [(r, d, c[0], c[1]) for (r, d), c in sorted(cube.items())])
    return {key: tuple(cell) for key, cell in cube.items()}


def main():
    try:
        cube = build_cube()
    except sqlite3.OperationalError:
        print("There is nothing to report yet. Start the app and place an order first.")
        return
    if not cube:
        print("No paid orders yet, so the cube is empty.")
        return
    print("%-20s %-12s %7s %14s" % ("Restaurant", "Day", "Orders", "Revenue (Rs)"))
    for (restaurant, day), (orders, revenue) in sorted(cube.items()):
        print("%-20s %-12s %7d %14s" % (restaurant, day, orders, format(revenue, ",")))


if __name__ == "__main__":
    main()
