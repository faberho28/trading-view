#!/usr/bin/env bash
# Bootstrap script for a fresh Ubuntu EC2 instance (22.04/24.04 LTS).
# Run as a user with sudo access, from the repo root after cloning it to /opt/trading-bot.
#
# Usage (on the instance, after `git clone <your-repo> /opt/trading-bot`):
#   cd /opt/trading-bot
#   sudo bash deploy/setup_ec2.sh
#
# This script does NOT create or copy your .env file — do that manually
# (scp it over, or paste it with a restrictive umask) so real API keys are
# never stored in shell history or committed to git.

set -euo pipefail

APP_DIR="/opt/trading-bot"
SERVICE_USER="trading-bot"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [ "$(pwd)" != "$APP_DIR" ]; then
  echo "Expected to run from $APP_DIR (repo checked out there). Current dir: $(pwd)" >&2
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

echo "==> Installing systemd unit"
cp "$APP_DIR/deploy/trading-bot.service" /etc/systemd/system/trading-bot.service
systemctl daemon-reload
systemctl enable trading-bot

echo "==> Done. Once .env is in place, start the service with:"
echo "    sudo systemctl start trading-bot"
echo "    sudo journalctl -u trading-bot -f"
