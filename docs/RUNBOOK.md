# Signal Track Runbook

## Production Layout

Recommended Linux layout:

```text
/srv/signal-track/app       # git checkout
/srv/signal-track/venv      # Python virtualenv
/srv/signal-track/shared    # env, SQLite DB, generated dashboard
```

Secrets live in:

```text
/srv/signal-track/shared/signal-track.env
```

Do not commit that file.

## Install

```bash
git clone https://github.com/liangzh77/signal_track /srv/signal-track/app
cd /srv/signal-track/app
sudo bash scripts/install_service.sh
sudo editor /srv/signal-track/shared/signal-track.env
sudo systemctl start signal-track.service
sudo systemctl start signal-track-daily.timer
```

Set `SIGNAL_TRACK_API_KEY` in `signal-track.env` before exposing the backend outside
localhost. Mutating endpoints reject requests without that key.

## Service Commands

```bash
sudo systemctl status signal-track.service
sudo journalctl -u signal-track.service -f
sudo systemctl restart signal-track.service
```

Daily job:

```bash
sudo systemctl status signal-track-daily.timer
sudo systemctl list-timers signal-track-daily.timer
sudo systemctl start signal-track-daily.service
sudo journalctl -u signal-track-daily.service -n 100
```

## Health Check

```bash
python scripts/healthcheck.py http://127.0.0.1:8765/health
```

Expected:

```json
{"ok": true, "status": 200, "body": {"ok": true}}
```

## Initial Data Setup

With Tushare token configured:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli refresh-instruments --provider tushare --market all
```

Without token, seed fixture symbols:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli refresh-instruments --provider fixture --market all
```

## Manual Input

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli ingest --source 信息源A --text "腾讯 做多，先跟踪。" --publish
```

File input:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli ingest --source 信息源A --file /path/to/note.md --publish
```

HTTP input with API key:

```bash
curl -X POST http://127.0.0.1:8765/api/inputs \
  -H "X-Signal-Track-Key: $SIGNAL_TRACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"信息源A","content":"腾讯 做多，先跟踪。"}'
```

## Manual Daily Run

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli daily-run --provider tushare --publish --out /srv/signal-track/shared/dashboard.html
```

## Backup

SQLite backup:

```bash
sqlite3 /srv/signal-track/shared/signal_track.sqlite3 ".backup '/srv/signal-track/shared/backup-$(date +%F).sqlite3'"
```

Keep `signal-track.env` outside git and back it up with your normal secret store.
