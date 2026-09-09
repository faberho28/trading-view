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
SERVICE_USER="trading-bot"

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
apt-get install -y python3.12 python3.12-venv python3-pip

echo "==> Creating dedicated service user (no login shell)"
if ! id "$SERVICE_USER" &>/dev/null; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo "==> Creating virtualenv and installing dependencies"
python3.12 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

echo "==> Preparing logs directory"
mkdir -p "$APP_DIR/logs"

echo "==> Fixing ownership and permissions"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
if [ -f "$APP_DIR/.env" ]; then
  chmod 600 "$APP_DIR/.env"
  chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.env"
else
  echo "WARNING: $APP_DIR/.env not found. Copy your paper-trading credentials there" >&2
  echo "         (chmod 600) before starting the service." >&2
fi

echo "==> Generating systemd unit for APP_DIR=$APP_DIR"
sed -e "s|/opt/trading-bot|$APP_DIR|g" \
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
