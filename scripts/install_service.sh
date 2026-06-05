#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/srv/signal-track/app}"
SHARED_DIR="${SHARED_DIR:-/srv/signal-track/shared}"
VENV_DIR="${VENV_DIR:-/srv/signal-track/venv}"
SERVICE_USER="${SERVICE_USER:-signal-track}"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo APP_DIR=$APP_DIR bash scripts/install_service.sh" >&2
  exit 1
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home /srv/signal-track --shell /usr/sbin/nologin "$SERVICE_USER"
fi

mkdir -p "$APP_DIR" "$SHARED_DIR"
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -e "${APP_DIR}[web,market,llm]"

if [ ! -f "$SHARED_DIR/signal-track.env" ]; then
  cp "$APP_DIR/deploy/env.production.example" "$SHARED_DIR/signal-track.env"
  chmod 600 "$SHARED_DIR/signal-track.env"
fi

cp "$APP_DIR/deploy/systemd/signal-track.service" /etc/systemd/system/signal-track.service
cp "$APP_DIR/deploy/systemd/signal-track-daily.service" /etc/systemd/system/signal-track-daily.service
cp "$APP_DIR/deploy/systemd/signal-track-daily.timer" /etc/systemd/system/signal-track-daily.timer

chown -R "$SERVICE_USER:$SERVICE_USER" /srv/signal-track
systemctl daemon-reload
systemctl enable signal-track.service signal-track-daily.timer

echo "Edit $SHARED_DIR/signal-track.env, then run:"
echo "  systemctl start signal-track.service"
echo "  systemctl start signal-track-daily.timer"
