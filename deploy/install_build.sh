#!/bin/bash
# Food Cat: install one unpacked build on this EC2 server and switch production to it.
#
#   bash install_build.sh /var/tmp/foodcat-deploy.XXXXXX/build-v1.1-7
#
# pull_latest_build.sh starts this script once it has fetched the latest build from S3.
# The first deploy also prepares the server (Python 3.12, a 'foodcat' user, a secret key and the
# systemd service), so a brand new Amazon Linux 2023 instance needs no setup by hand. Later deploys
# skip those steps. If the new build does not answer within a minute, the server goes back to the
# build that ran before, and the pipeline turns red.
set -euo pipefail

SRC=$(cd "$1" && pwd)
APP=/opt/foodcat
DATA=/var/lib/foodcat
ENV_FILE=/etc/foodcat/foodcat.env
PYTHON=python3.12
CHECK_URL=http://127.0.0.1/api/version

say() { echo "[deploy] $*"; }

# 1. Prepare the server (real work happens only on the first deploy)
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  say "First deploy: installing Python 3.12"
  dnf install -y -q python3.12 python3.12-pip >/dev/null
fi
if ! id foodcat >/dev/null 2>&1; then
  say "First deploy: creating the foodcat user (the app never runs as root)"
  useradd --system --no-create-home --home-dir "$DATA" --shell /sbin/nologin foodcat
fi
install -d -m 755 "$APP" "$APP/releases"
install -d -m 750 -o foodcat -g foodcat "$DATA"
if [ ! -f "$ENV_FILE" ]; then
  say "First deploy: making a secret key for the login tokens"
  install -d -m 700 /etc/foodcat
  (umask 077 && printf 'SECRET_KEY=%s\n' "$("$PYTHON" -c 'import secrets; print(secrets.token_hex(32))')" > "$ENV_FILE")
fi
install -m 644 "$SRC/deploy/foodcat.service" /etc/systemd/system/foodcat.service
systemctl daemon-reload
systemctl enable --quiet foodcat

# 2. Put the new build next to the older ones, with its own Python packages
BUILD=$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["build_name"])' "$SRC/build-info.json")
DEST="$APP/releases/$BUILD-$(date -u +%Y%m%d%H%M%S)"
say "Installing $BUILD"
cp -r "$SRC" "$DEST"
"$PYTHON" -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --quiet --disable-pip-version-check --no-input -r "$DEST/requirements.txt"
"$DEST/.venv/bin/python" -m compileall -q "$DEST/foodcat" "$DEST/run_local.py" >/dev/null
chown -R root:root "$DEST"
chmod -R go-w "$DEST"

# 3. Switch production to the new build
PREVIOUS=$(readlink -e "$APP/current" || true)
ln -sfn "$DEST" "$APP/current"
systemctl restart foodcat
say "Restarted Food Cat, waiting for $BUILD to answer"

# 4. Check that the new build really answers
answers() {
  curl -fsS --max-time 3 "$CHECK_URL" 2>/dev/null |
    "$PYTHON" -c 'import json, sys; sys.exit(json.load(sys.stdin).get("build_name") != sys.argv[1])' "$1" 2>/dev/null
}
for _ in $(seq 1 30); do
  if answers "$BUILD"; then
    say "Production now runs $BUILD"
    # Keep the three newest builds, so there is always an older one to go back to.
    find "$APP/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | tail -n +4 | cut -d' ' -f2- | xargs -r rm -rf
    exit 0
  fi
  sleep 2
done

say "$BUILD did not answer in time. The last lines of its log:"
journalctl -u foodcat -n 30 --no-pager || true
if [ -n "$PREVIOUS" ] && [ -d "$PREVIOUS" ] && [ "$PREVIOUS" != "$DEST" ]; then
  OLD=$("$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1]))["build_name"])' "$PREVIOUS/build-info.json")
  say "Rolling back to $OLD"
  ln -sfn "$PREVIOUS" "$APP/current"
  systemctl restart foodcat
  rm -rf "$DEST"
  for _ in $(seq 1 30); do
    if answers "$OLD"; then
      say "Rolled back: production runs $OLD again"
      break
    fi
    sleep 2
  done
fi
exit 1
