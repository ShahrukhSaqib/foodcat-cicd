"""The small helpers around the services: logging, the worker loop, folders, the launcher and the demo script."""
import io
import json
import sys
import threading
import time
import urllib.request

import pytest
from werkzeug.serving import make_server

import demo
import run_local
from foodcat import alert_lambda, common, delivery_service, payment_service

from conftest import DHA


def test_logging_survives_a_console_that_cannot_show_the_text(monkeypatch):
    ascii_only = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", ascii_only)
    common.log("user", "new customer signed up: عائشہ")   # an Urdu name must not break a request
    ascii_only.flush()


def test_a_worker_loop_survives_an_error_and_keeps_going(monkeypatch, capsys):
    monkeypatch.setattr(common.time, "sleep", lambda seconds: None)
    calls = []

    def step():
        calls.append(len(calls))
        if len(calls) == 1:
            raise ValueError("one bad message")
        if len(calls) == 2:
            return 1
        raise SystemExit  # the only way to stop an endless loop in a test

    with pytest.raises(SystemExit):
        common.loop_forever(step)
    assert calls == [0, 1, 2]
    assert "unexpected error" in capsys.readouterr().out


def test_the_queue_folder_can_be_set_apart_from_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("FOODCAT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("FOODCAT_QUEUE_DIR", raising=False)
    assert common.queue_dir() == common.data_dir() == (tmp_path / "data").resolve()
    monkeypatch.setenv("FOODCAT_QUEUE_DIR", str(tmp_path / "queues"))
    assert common.queue_dir() == (tmp_path / "queues").resolve() and (tmp_path / "queues").is_dir()


def test_run_starts_the_worker_and_listens_where_the_settings_say(monkeypatch):
    started = threading.Event()
    seen = {}

    class FakeApp:
        def run(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setenv("PORT", "5999")
    common.run(FakeApp(), 5001, worker=started.set)
    assert started.wait(2)
    assert seen == {"host": "127.0.0.1", "port": 5999, "threaded": True, "debug": False}
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.delenv("PORT")
    common.run(FakeApp(), 5001)
    assert seen["host"] == "0.0.0.0" and seen["port"] == 5001


def test_the_launcher_reads_a_dotenv_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('# a comment\n\nSECRET_KEY="quoted-value"\nOTHER = plain \nbroken line\n', encoding="utf-8")
    assert run_local.read_env_file(env) == {"SECRET_KEY": "quoted-value", "OTHER": "plain"}
    assert run_local.read_env_file(tmp_path / "missing") == {}


def test_the_launcher_can_tell_that_nothing_is_listening():
    assert run_local.is_up(1) is False


def no_proxy_please(monkeypatch):
    """Pretend the computer is set up to send everything through a proxy that does not exist."""
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens on port 9
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


def test_the_launcher_does_not_use_a_proxy_to_check_its_own_programs(monkeypatch):
    no_proxy_please(monkeypatch)
    urllib.request.install_opener(None)  # forget any opener that an earlier test built before the proxy was set
    probe = make_server("127.0.0.1", 0, common.make_service("probe"), threaded=True)
    threading.Thread(target=probe.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    try:
        assert run_local.is_up(probe.server_port) is True
    finally:
        probe.shutdown()
        probe.server_close()


@pytest.fixture
def live(stack, monkeypatch):
    """The whole system, ready for the demo script: the three background workers keep running and the gateway has a real address."""
    monkeypatch.setattr(demo, "POLL_SECONDS", 0.05)  # the demo can look again quickly, nobody is watching
    stop = threading.Event()

    def keep_working(step):
        while not stop.is_set():
            if not step(stack.folder):
                time.sleep(0.05)

    for step in (payment_service.process_once, delivery_service.process_once, alert_lambda.process_once):
        threading.Thread(target=keep_working, args=(step,), daemon=True).start()
    door = make_server("127.0.0.1", 0, stack.app, threaded=True)
    threading.Thread(target=door.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    yield demo.Api("http://127.0.0.1:%d" % door.server_port)
    stop.set()
    door.shutdown()
    door.server_close()


def test_the_demo_script_plays_through_a_live_system(live, capsys, monkeypatch):
    no_proxy_please(monkeypatch)  # the demo must talk to our own computer directly
    demo.run(live, break_cache=False)
    out = capsys.readouterr().out
    assert "Demo finished. Everything worked." in out
    assert "Sara got nothing because Ali is the nearest rider" in out
    assert "no rider was sent" in out


def test_the_demo_can_be_played_twice_on_the_same_system(live, capsys):
    demo.run(live, break_cache=False)
    demo.run(live, break_cache=False)   # last time's Ali must not still be online and steal the job
    assert capsys.readouterr().out.count("Demo finished. Everything worked.") == 2


def test_the_demo_riders_go_offline_when_it_ends(live, stack):
    demo.run(live, break_cache=False)
    with common.db("delivery", stack.folder) as conn:
        online = [json.loads(row["doc"])["online"] for row in conn.execute("SELECT doc FROM riders")]
    assert len(online) == 2 and not any(online)


def test_the_demo_can_rehearse_a_dead_cache(live, stack, capsys, monkeypatch):
    ports = tuple(int(url.rsplit(":", 1)[1]) for url in stack.urls["order"])
    monkeypatch.setattr(demo, "ORDER_SERVICE_PORTS", ports)
    demo.run(live, break_cache=True)
    out = capsys.readouterr().out
    assert "the menu still works: read from the database (cache is down)" in out
    assert "cache repaired" in out and "Demo finished. Everything worked." in out


def test_the_cache_rehearsal_says_that_it_needs_the_local_run(live, monkeypatch):
    monkeypatch.setattr(demo, "ORDER_SERVICE_PORTS", (1,))   # nothing listens there, like in Docker
    with pytest.raises(demo.DemoFailed) as problem:
        demo.run(live, break_cache=True)
    assert "python run_local.py" in str(problem.value) and "Docker" in str(problem.value)


def test_the_demo_explains_what_to_do_when_another_rider_is_nearer(live, stack, monkeypatch):
    monkeypatch.setattr(demo, "JOB_ALERT_SECONDS", 1)
    token, _ = stack.join("Bilal", "rider")
    stack.rider_online(token, "Bilal", DHA)   # DHA is where Burger Lab is, so Bilal is even closer than Ali
    with pytest.raises(demo.DemoFailed) as problem:
        demo.run(live, break_cache=False)
    assert "python run_local.py --fresh" in str(problem.value) and "Bilal" in str(problem.value)


def test_the_demo_explains_when_nothing_is_running(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["demo.py", "--base", "http://127.0.0.1:1"])
    with pytest.raises(SystemExit) as stopped:
        demo.main()
    assert "python run_local.py" in str(stopped.value)
