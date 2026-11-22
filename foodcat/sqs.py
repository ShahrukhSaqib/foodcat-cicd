"""A tiny stand-in for Amazon SQS.

It works the way SQS works, so the services don't care which one is behind it:
  * send()     puts a message on the queue,
  * receive()  hands out messages and hides them for a while (the 'visibility timeout'),
  * delete()   removes a message for good once the work succeeded.

If a consumer crashes and never calls delete(), the message becomes visible again
after the timeout and somebody else retries it. That is the 'at least once' promise.
Because a message can arrive twice, every consumer in this project is idempotent
(handling the same message twice has the same result as handling it once).

Locally the messages live in one SQLite file so the app runs on any laptop.
On AWS you would replace this file with boto3 calls to a real SQS queue.
"""
import json
import os
import time
import uuid

from .common import db, log, queue_dir, transaction

NEW_ORDERS = "new-orders"    # Order Service → Payment Service
PAID_ORDERS = "paid-orders"  # Payment Service → Delivery Service
UPDATES = "updates"          # Payment and Delivery → Lambda (alerts)
DEAD_LETTER = "dead-letter"  # messages that failed too many times

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    queue         TEXT NOT NULL,
    body          TEXT NOT NULL,
    created       REAL NOT NULL,
    visible_at    REAL NOT NULL,
    receive_count INTEGER NOT NULL DEFAULT 0,
    receipt       TEXT
)
"""


class Retry(Exception):
    """A handler raises this to say 'not yet, ask me again later'. It is not a failure,
    so the message is never sent to the dead letter queue because of it."""


class Message:
    def __init__(self, id, body, receive_count, receipt):
        self.id = id
        self.body = body
        self.receive_count = receive_count
        self.receipt = receipt


class Queue:
    def __init__(self, name, folder=None, visibility_timeout=None):
        self.name = name
        self.folder = folder or queue_dir()
        if visibility_timeout is None:
            visibility_timeout = float(os.environ.get("FOODCAT_VISIBILITY_TIMEOUT", "5"))
        self.visibility_timeout = visibility_timeout
        with db("queues", self.folder) as conn:
            conn.execute(_SCHEMA)

    def send(self, body):
        """Put a message (a dict) on the queue and return its id."""
        now = time.time()
        with db("queues", self.folder) as conn:
            cur = conn.execute(
                "INSERT INTO messages (queue, body, created, visible_at) VALUES (?, ?, ?, ?)",
                (self.name, json.dumps(body), now, now),
            )
            return cur.lastrowid

    def receive(self, max_messages=1):
        """Take up to max_messages. They stay hidden until the visibility timeout ends."""
        now = time.time()
        found = []
        with db("queues", self.folder) as conn:
            with transaction(conn):  # two consumers can never grab the same message
                rows = conn.execute(
                    "SELECT id, body, receive_count FROM messages "
                    "WHERE queue = ? AND visible_at <= ? ORDER BY id LIMIT ?",
                    (self.name, now, max_messages),
                ).fetchall()
                for row in rows:
                    receipt = uuid.uuid4().hex
                    conn.execute(
                        "UPDATE messages SET visible_at = ?, receive_count = receive_count + 1, receipt = ? WHERE id = ?",
                        (now + self.visibility_timeout, receipt, row["id"]),
                    )
                    found.append(Message(row["id"], json.loads(row["body"]), row["receive_count"] + 1, receipt))
        return found

    def delete(self, message):
        """Work finished. Only the latest receipt can delete (like an SQS receipt handle)."""
        with db("queues", self.folder) as conn:
            conn.execute("DELETE FROM messages WHERE id = ? AND receipt = ?", (message.id, message.receipt))

    def size(self):
        """Messages waiting plus messages being worked on."""
        with db("queues", self.folder) as conn:
            return conn.execute("SELECT COUNT(*) FROM messages WHERE queue = ?", (self.name,)).fetchone()[0]


def consume(queue, handler, batch=5, max_receives=5, service="worker"):
    """The shared consumer step used by Payment, Delivery and the alert Lambda.

    handler(body) does the real work. If it raises, the message is left alone and comes
    back after the visibility timeout. After max_receives failures it goes to the dead
    letter queue, so one poison message can't block everything behind it.
    Returns how many messages were handled successfully.
    """
    done = 0
    for msg in queue.receive(batch):
        try:
            handler(msg.body)
            queue.delete(msg)
            done += 1
        except Retry as why:
            log(service, "message %s will be tried again later: %s" % (msg.id, why))
        except Exception as exc:
            log(service, "message %s failed (attempt %s): %r" % (msg.id, msg.receive_count, exc))
            if msg.receive_count >= max_receives:
                Queue(DEAD_LETTER, queue.folder).send({"from": queue.name, "body": msg.body, "error": repr(exc)})
                queue.delete(msg)
                log(service, "message %s moved to the dead letter queue" % msg.id)
    return done
