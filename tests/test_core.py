"""The building blocks: passwords, tokens, the queue, the cache."""
import threading
import time

import pytest

from foodcat import sqs
from foodcat.cache import CacheDown, TTLCache
from foodcat.common import check_password, db, hash_password, make_token, read_token, transaction


# ---------------------------------------------------------------- passwords and tokens

def test_password_is_hashed_and_checked():
    stored = hash_password("secret123")
    assert "secret123" not in stored
    assert check_password("secret123", stored)
    assert not check_password("secret124", stored)


def test_real_passwords_use_the_strong_setting_and_old_hashes_keep_working(monkeypatch):
    monkeypatch.setenv("PASSWORD_ROUNDS", "1000")
    old = hash_password("secret123")
    monkeypatch.delenv("PASSWORD_ROUNDS")              # back to the default
    stored = hash_password("secret123")
    assert stored.startswith("600000$")                # the number OWASP advises today
    assert check_password("secret123", stored) and not check_password("wrong", stored)
    assert old.startswith("1000$") and check_password("secret123", old)   # the rounds travel inside the hash


def test_same_password_gets_a_different_hash_each_time():
    assert hash_password("secret123") != hash_password("secret123")


def test_token_round_trip():
    payload = read_token(make_token(7, "rider"))
    assert payload["uid"] == 7 and payload["role"] == "rider"


def test_tampered_token_is_refused():
    body, signature = make_token(7, "customer").split(".")
    forged = make_token(7, "rider").split(".")[0] + "." + signature  # a rider's body with a customer's signature
    assert read_token(forged) is None
    assert read_token(body + ".AAAA") is None
    assert read_token("not a token") is None
    assert read_token("") is None


def test_expired_token_is_refused(expired_token):
    assert read_token(expired_token) is None


def test_token_from_another_key_is_refused(monkeypatch):
    token = make_token(1, "customer")
    monkeypatch.setenv("SECRET_KEY", "a-completely-different-key-123456")
    assert read_token(token) is None


@pytest.mark.parametrize("value", ["", "short", "change-me-then-delete-this-comment"])
def test_the_system_refuses_to_run_without_a_real_secret(monkeypatch, value):
    monkeypatch.setenv("SECRET_KEY", value)
    with pytest.raises(RuntimeError):
        make_token(1, "customer")


def test_there_is_no_default_secret(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError):
        make_token(1, "customer")
    assert read_token("anything.at-all") is None   # and nobody can log in while the key is missing


# ---------------------------------------------------------------- the queue

def test_messages_come_out_in_order(folder):
    queue = sqs.Queue("q", folder)
    for n in range(3):
        queue.send({"n": n})
    assert [m.body["n"] for m in queue.receive(10)] == [0, 1, 2]


def test_a_message_in_flight_is_hidden_from_other_consumers(folder):
    queue = sqs.Queue("q", folder, visibility_timeout=60)
    queue.send({"n": 1})
    first = queue.receive()
    assert len(first) == 1
    assert queue.receive() == []          # hidden while somebody is working on it
    assert queue.size() == 1              # but it still exists


def test_a_message_that_was_never_deleted_comes_back(folder):
    queue = sqs.Queue("q", folder, visibility_timeout=0)
    queue.send({"n": 1})
    first = queue.receive()[0]
    second = queue.receive()[0]
    assert second.id == first.id and second.receive_count == 2


def test_only_the_latest_receipt_can_delete(folder):
    queue = sqs.Queue("q", folder, visibility_timeout=0)
    queue.send({"n": 1})
    old = queue.receive()[0]
    new = queue.receive()[0]
    queue.delete(old)                     # the old receipt is stale, nothing happens
    assert queue.size() == 1
    queue.delete(new)
    assert queue.size() == 0


def test_queues_do_not_see_each_others_messages(folder):
    a, b = sqs.Queue("a", folder), sqs.Queue("b", folder)
    a.send({"for": "a"})
    assert b.receive() == [] and b.size() == 0 and a.size() == 1


def test_consume_deletes_after_success(folder):
    queue = sqs.Queue("q", folder)
    queue.send({"n": 1})
    seen = []
    assert sqs.consume(queue, seen.append) == 1
    assert seen == [{"n": 1}] and queue.size() == 0


def test_a_failing_message_is_retried_then_parked_in_the_dead_letter_queue(folder):
    queue = sqs.Queue("q", folder)
    queue.send({"n": 1})
    attempts = []

    def always_fails(body):
        attempts.append(body)
        raise ValueError("broken")

    handled = [sqs.consume(queue, always_fails, max_receives=3) for _ in range(5)]
    assert handled == [0, 0, 0, 0, 0]
    assert len(attempts) == 3             # tried three times, then given up on
    assert queue.size() == 0
    parked = sqs.Queue(sqs.DEAD_LETTER, folder).receive()[0].body
    assert parked["from"] == "q" and parked["body"] == {"n": 1} and "broken" in parked["error"]


def test_a_retry_request_is_not_a_failure(folder):
    queue = sqs.Queue("q", folder)
    queue.send({"n": 1})

    def not_yet(body):
        raise sqs.Retry("come back later")

    for _ in range(10):
        sqs.consume(queue, not_yet, max_receives=3)
    assert queue.size() == 1              # still waiting, never parked
    assert sqs.Queue(sqs.DEAD_LETTER, folder).size() == 0


def test_one_bad_message_does_not_block_the_good_ones(folder):
    queue = sqs.Queue("q", folder)
    queue.send({"bad": True})
    queue.send({"bad": False})
    seen = []

    def handler(body):
        if body["bad"]:
            raise ValueError("no")
        seen.append(body)

    sqs.consume(queue, handler)
    assert seen == [{"bad": False}]


# ---------------------------------------------------------------- the cache

def test_cache_keeps_a_value_until_it_expires():
    cache = TTLCache(ttl=0.05)
    cache.set("menu", [1, 2])
    assert cache.get("menu") == [1, 2]
    time.sleep(0.08)
    assert cache.get("menu") is None


def test_cache_miss_is_none():
    assert TTLCache().get("nothing") is None


def test_a_dead_cache_raises_so_callers_can_fall_back():
    cache = TTLCache()
    cache.down = True
    with pytest.raises(CacheDown):
        cache.get("menu")
    with pytest.raises(CacheDown):
        cache.set("menu", 1)


def test_clear_empties_the_cache():
    cache = TTLCache()
    cache.set("a", 1)
    cache.clear()
    assert cache.get("a") is None


# ---------------------------------------------------------------- transactions

def test_a_failed_transaction_changes_nothing(folder):
    with db("t", folder) as conn:
        conn.execute("CREATE TABLE x (n INTEGER)")
        with pytest.raises(RuntimeError):
            with transaction(conn):
                conn.execute("INSERT INTO x VALUES (1)")
                raise RuntimeError("stop")
        assert conn.execute("SELECT COUNT(*) FROM x").fetchone()[0] == 0
        with transaction(conn):
            conn.execute("INSERT INTO x VALUES (2)")
        assert conn.execute("SELECT COUNT(*) FROM x").fetchone()[0] == 1


# ---------------------------------------------------------------- starting together

def test_many_programs_can_open_a_brand_new_database_in_the_same_moment(folder):
    """Docker starts every container at once. Before this was fixed, one of them now and then died with 'database is locked'."""
    problems = []
    for number in range(100):
        gate = threading.Barrier(8)

        def open_it(name="fresh%d" % number, gate=gate):
            gate.wait()
            try:
                with db(name, folder):
                    pass
            except Exception as exc:
                problems.append(repr(exc))

        threads = [threading.Thread(target=open_it) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert problems == []
