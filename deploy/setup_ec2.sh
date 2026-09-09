#!/usr/bin/env bash
# Bootstrap script for a fresh Ubuntu EC2 instance (22.04/24.04 LTS).
# Run as a user with sudo access, FROM THE REPO ROOT — wherever you cloned it
# (no need to move it to /opt first).
#
# Usage (on the instance, after `git clone <your-repo>`):
#   cd trading-view
#   sudo bash deploy/setup_ec2.sh
#
# This script does NOT create or copy your .env file — do that manually
# (scp it over, or paste it with a restrictive umask) so real API keys are
# never stored in shell history or committed to git.

set -euo pipefail

APP_DIR="$(pwd)"

# Under /home/<user>, that user's home directory is typically not traversable
# by other accounts (mode 750), so a dedicated system user can't chdir into
# it even without ProtectHome — systemd fails with status=200/CHDIR. Run as
# the repo's own owner in that case instead of creating a separate account.
case "$APP_DIR" in
  /home/*)
    SERVICE_USER="$(stat -c '%U' "$APP_DIR")"
    CREATE_SERVICE_USER=false
    ;;
  *)
    SERVICE_USER="trading-bot"
    CREATE_SERVICE_USER=true
    ;;
esac

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [ ! -f "$APP_DIR/requirements.txt" ] || [ ! -d "$APP_DIR/src/trading_bot" ]; then
  echo "Run this from the repo root (requirements.txt / src/trading_bot not found in $APP_DIR)." >&2
  exit 1
fi

echo "==> Installing system dependencies"
apt-get update -y
apt-get install -y software-properties-common python3-pip

if ! apt-cache show python3.12 &>/dev/null; then
  echo "    python3.12 not in default repos (Ubuntu 22.04 ships 3.10) — adding deadsnakes PPA"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -y
fi

apt-get install -y python3.12 python3.12-venv

if [ "$CREATE_SERVICE_USER" = true ]; then
  echo "==> Creating dedicated service user (no login shell)"
  if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
else
  echo "==> Running as existing user '$SERVICE_USER' (owner of $APP_DIR)"
fi

echo "==> Creating virtualenv and installing dependencies"
python3.12 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "==> Preparing logs directory"
mkdir -p "$APP_DIR/logs"

SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

echo "==> Fixing ownership and permissions"
# Idempotent even on a re-run after a previous attempt used a different
# SERVICE_USER (e.g. a prior /opt-style run left files owned by a
# now-unused 'trading-bot' account).
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR"
if [ -f "$APP_DIR/.env" ]; then
  chmod 600 "$APP_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_GROUP" "$APP_DIR/.env"
else
  echo "WARNING: $APP_DIR/.env not found. Copy your paper-trading credentials there" >&2
  echo "         (chmod 600) before starting the service." >&2
fi

echo "==> Generating systemd unit for APP_DIR=$APP_DIR (user=$SERVICE_USER)"
sed -e "s|/opt/trading-bot|$APP_DIR|g" \
    -e "s|^User=.*|User=$SERVICE_USER|" \
    -e "s|^Group=.*|Group=$SERVICE_GROUP|" \
  "$APP_DIR/deploy/trading-bot.service" > /etc/systemd/system/trading-bot.service

case "$APP_DIR" in
  /home/*)
    echo "    NOTE: repo lives under /home — disabling ProtectHome so the"
    echo "    service can actually read it (systemd hides /home otherwise)."
    sed -i '/^ProtectHome=/d' /etc/systemd/system/trading-bot.service
    ;;
esac

systemctl daemon-reload
systemctl enable trading-bot

echo "==> Done. Once .env is in place, start the service with:"
echo "    sudo systemctl start trading-bot"
echo "    sudo journalctl -u trading-bot -f"
