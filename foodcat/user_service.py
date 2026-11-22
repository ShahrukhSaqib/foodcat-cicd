"""User Service: sign-up and login. It owns the User DB (users.db) and nothing else."""
import sqlite3
import time

from flask import jsonify, request

from .common import (check_password, current_user, db, error, hash_password, log,
                     make_service, make_token, require_secret, run)

# The restaurant portal from the diagram is not built in this running version.
ROLES = ("customer", "rider")


def create_app(folder=None, instance=None):
    app = make_service("user-service", instance)

    with db("users", folder) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   name TEXT NOT NULL,
                   email TEXT NOT NULL UNIQUE,
                   password_hash TEXT NOT NULL,
                   role TEXT NOT NULL,
                   created REAL NOT NULL)"""
        )

    @app.post("/signup")
    def signup():
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        role = data.get("role") or "customer"
        if not name or len(name) > 40 or "@" not in email or len(email) > 100 or len(password) < 6:
            return error("Please give a name (up to 40 characters), a valid email and a password of at least 6 characters.", 400)
        if role not in ROLES:
            return error("Role must be one of: " + ", ".join(ROLES), 400)
        try:
            with db("users", folder) as conn:
                cur = conn.execute(
                    "INSERT INTO users (name, email, password_hash, role, created) VALUES (?, ?, ?, ?, ?)",
                    (name, email, hash_password(password), role, time.time()),
                )
                user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            return error("That email is already registered.", 409)
        log("user", "new %s signed up: %s (id %s)" % (role, name, user_id))
        return jsonify(id=user_id, name=name, role=role), 201

    @app.post("/login")
    def login():
        data = request.get_json(silent=True) or {}
        email = str(data.get("email", "")).strip().lower()
        password = str(data.get("password", ""))
        with db("users", folder) as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        # Same message for 'no such email' and 'wrong password', so nobody can probe for accounts.
        if row is None or not check_password(password, row["password_hash"]):
            return error("Wrong email or password.", 401)
        token = make_token(row["id"], row["role"])
        log("user", "%s logged in" % row["name"])
        return jsonify(token=token, user={"id": row["id"], "name": row["name"], "role": row["role"]})

    @app.get("/me")
    def me():
        user = current_user()
        if not user:
            return error("Please log in first.", 401)
        with db("users", folder) as conn:
            row = conn.execute("SELECT id, name, email, role FROM users WHERE id = ?", (user["id"],)).fetchone()
        if row is None:
            return error("No such user.", 404)
        return jsonify(dict(row))

    return app


def main():
    require_secret()
    run(create_app(), 5001)


if __name__ == "__main__":
    main()
