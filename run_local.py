"""Start the whole Food Cat system on this computer with one command:

    python run_local.py            (add --fresh to wipe the old data first)

It starts seven small programs (the gateway, the user, payment, delivery and alert
services, and two copies of the order service), waits until each one answers /health,
and then prints the address. Press Ctrl+C to stop them all.

The AWS server starts Food Cat with the same launcher (see deploy/foodcat.service):

    python run_local.py --host 0.0.0.0 --port 80 --data-dir /var/lib/foodcat

Only the gateway listens on that address and port. The other six programs always stay
on 127.0.0.1, so the internet can reach Food Cat through the front door only.
"""
import argparse
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # our own programs are never reached through a proxy

# (python module, port, instance name)
PROGRAMS = [
    ("gateway", 8000, "gateway"),
    ("user_service", 5001, "user-service"),
    ("order_service", 5002, "order-1"),
    ("order_service", 5012, "order-2"),
    ("payment_service", 5003, "payment-service"),
    ("delivery_service", 5004, "delivery-service"),
    ("alert_lambda", 5005, "alerts"),
]


def read_env_file(path):
    """Read KEY=VALUE lines from a .env file (blank lines and # comments are skipped)."""
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def stop_on_terminate(signum, frame):
    """A plain 'kill' should stop the seven programs too, not leave them running behind."""
    raise KeyboardInterrupt


def is_up(port):
    try:
        with NO_PROXY.open("http://127.0.0.1:%d/health" % port, timeout=1) as reply:
            return reply.status == 200
    except OSError:
        return False


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run Food Cat locally.")
    parser.add_argument("--fresh", action="store_true", help="delete the old data folder first")
    parser.add_argument("--host", default="127.0.0.1",
                        help="address the gateway listens on (0.0.0.0 on the AWS server). The other services stay on 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="port of the gateway (80 on the AWS server)")
    parser.add_argument("--data-dir", default=str(ROOT / "data"),
                        help="folder for the database files (on the server it lives outside the build, so a deploy keeps the data)")
    return parser.parse_args(argv)


def programs_for(gateway_port):
    """The seven programs, with the gateway on the chosen port."""
    return [(module, gateway_port if module == "gateway" else port, instance) for module, port, instance in PROGRAMS]


def main():
    args = parse_args()
    signal.signal(signal.SIGTERM, stop_on_terminate)

    data = Path(args.data_dir).resolve()
    if args.fresh and data.exists():
        shutil.rmtree(data)
    data.mkdir(parents=True, exist_ok=True)
    programs = programs_for(args.port)

    settings = read_env_file(ROOT / ".env")
    secret = os.environ.get("SECRET_KEY") or settings.get("SECRET_KEY", "")
    if not secret or secret.startswith("change-me"):
        secret = secrets.token_hex(32)  # a fresh key for this run, so people just have to log in again next time
        print("No usable SECRET_KEY in .env, so I made a random one for this run.")

    base = dict(os.environ, **settings)
    base.update(SECRET_KEY=secret, FOODCAT_DATA_DIR=str(data), PYTHONUNBUFFERED="1")
    for key in ("PORT", "HOST", "INSTANCE_NAME", "FOODCAT_QUEUE_DIR"):
        base.pop(key, None)  # these are set per program below, or left out on purpose

    children = []
    try:
        for module, port, instance in programs:
            env = dict(base, PORT=str(port), INSTANCE_NAME=instance)
            if module == "gateway":
                env["HOST"] = args.host  # only the front door may listen beyond this computer
            children.append(subprocess.Popen([sys.executable, "-m", "foodcat." + module], cwd=ROOT, env=env))

        deadline = time.time() + 30
        waiting = {port: module for module, port, _ in programs}
        while waiting:
            for port in list(waiting):
                if is_up(port):
                    del waiting[port]
            dead = [c for c in children if c.poll() is not None]
            if dead:
                raise SystemExit("A service stopped while starting. Read the messages above to see why.")
            if waiting and time.time() > deadline:
                raise SystemExit("These services did not start in time: " + ", ".join(waiting.values()))
            time.sleep(0.3)

        print()
        if args.host in ("127.0.0.1", "localhost"):
            print("Food Cat is running.  Open http://127.0.0.1:%d in your browser." % args.port)
            print("Run 'python demo.py' in another window for a guided demo. Press Ctrl+C here to stop.")
        else:
            print("Food Cat is running. The gateway listens on %s, port %d." % (args.host, args.port))
        print()
        while all(c.poll() is None for c in children):
            time.sleep(1)
        print("One service stopped, so I am stopping the rest.")
    except KeyboardInterrupt:
        print("\nStopping Food Cat.")
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
