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

The production template uses the systemd timer for scheduled checks and keeps
`SIGNAL_TRACK_ENABLE_SCHEDULER=false` to avoid running the same job twice. If you
prefer the FastAPI process to own scheduling, do not start
`signal-track-daily.timer` and set `SIGNAL_TRACK_ENABLE_SCHEDULER=true`.

## Service Commands

```bash
sudo systemctl status signal-track.service
sudo journalctl -u signal-track.service -f
sudo systemctl restart signal-track.service
```

Daily jobs:

```bash
sudo systemctl status signal-track-daily.timer
sudo systemctl list-timers signal-track-daily.timer
sudo systemctl start signal-track-daily.service
sudo journalctl -u signal-track-daily.service -n 100
```

The timer runs at 19:00 Asia/Shanghai for the China/Hong Kong trading day and at
07:00 Asia/Shanghai as a US-market catch-up pass. The CLI job is idempotent for a
given project/date; the later run updates the same daily check row when needed.

## Health Check

```bash
python scripts/healthcheck.py http://127.0.0.1:8765/health
```

When the in-process scheduler is enabled, `/health` includes `scheduler_jobs`.
Verify that `asia_evening_daily_check` and `us_morning_daily_check` are both
present before relying on the backend scheduler instead of the systemd timer.

Expected shape:

```json
{
  "ok": true,
  "status": 200,
  "body": {
    "ok": true,
    "database": {"ok": true},
    "degraded_reasons": [],
    "projects": {"total": 0, "active_or_review": 0, "exit_signal": 0, "needs_review": 0},
    "latest_check": null,
    "latest_publish": null
  }
}
```

If the latest dashboard publish attempt failed, `/health` returns `ok: false`
with `degraded_reasons: ["latest_publish_failed"]`, so `scripts/healthcheck.py`
exits non-zero.

Local smoke check without touching the production database:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli self-check --provider fixture --out /tmp/signal-track-self-check.html
```

Market data coverage preflight without calling remote market APIs:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli market-coverage --provider auto
curl http://127.0.0.1:8765/api/market-data/coverage?provider=auto
```

Before relying on the daily job, confirm the report marks the required markets as
`price_available: true`. A shares and China futures require Tushare credentials;
Hong Kong futures and US futures require the yfinance package or a future
licensed futures adapter.

Then run a real sample fetch against the configured provider:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli market-smoke --provider auto --market all --days 30
curl "http://127.0.0.1:8765/api/market-data/smoke?provider=auto&market=all&days=30"
```

The smoke result should return `ok: true` and non-zero `bar_count` values for the
markets you intend to trade. Use `--provider fixture` only to verify local wiring
without remote market API calls.
`SIGNAL_TRACK_DAILY_PROVIDER` defaults to `auto`; set it to `none` only for
offline rule checks that should not refresh prices.
OpenAI logic extraction and daily evaluation do not use live web research by
default. Set `SIGNAL_TRACK_OPENAI_WEB_RESEARCH=true` and use a web-search-capable
`SIGNAL_TRACK_OPENAI_MODEL` when you want weak-logic supplements and daily logic
checks to force Responses API web search. The optional
`SIGNAL_TRACK_OPENAI_WEB_SEARCH_CONTEXT_SIZE` value can be `low`, `medium`, or
`high`.

## Initial Data Setup

With provider credentials configured:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli refresh-instruments --provider auto --market all
```

Without token, seed fixture symbols:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli refresh-instruments --provider fixture --market all
```

## Manual Input

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli ingest --source 信息源A --text "腾讯 做多，先跟踪。"
```

File input:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli ingest --source 信息源A --file /path/to/note.md
```

HTTP input with API key:

```bash
curl -X POST http://127.0.0.1:8765/api/inputs \
  -H "X-Signal-Track-Key: $SIGNAL_TRACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"信息源A","content":"腾讯 做多，先跟踪。"}'
```

Source is required for ingestion. If the caller omits `source`, the note must
include a first-line marker such as `source: Alpha Desk`, `来源：Alpha Desk`, or
`信息源：Alpha Desk`; otherwise the service returns `source_required` and does
not create a tracking project.

Portfolio notes can either pass `--portfolio` / `"portfolio": true`, or include
an explicit marker in the note such as `组合`, `portfolio`, `权重`, or `占比`.
Plain multi-instrument notes without those markers are split into separate
tracking projects.

If a portfolio was created with equal weights and `weight_needs_review`, update
the weights after confirmation:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli list-projects --status needs_review --direction long
/srv/signal-track/venv/bin/python -m signal_track.cli export-project-report 1 --out /srv/signal-track/shared/project-1.md
/srv/signal-track/venv/bin/python -m signal_track.cli update-project-weights 1 --weights-json '{"300750.SZ":60,"600519.SH":40}'
/srv/signal-track/venv/bin/python -m signal_track.cli add-project-note 1 --text "manual observation: ads recovered" --type manual_note
```

## Manual Daily Run

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli daily-run --out /srv/signal-track/shared/dashboard.html
```

Daily checks write project-level review reasons into `triggered_rules` when a
project still has weak opening logic or provisional equal-weight portfolio
weights. This is expected: price data can be healthy while the project remains
`needs_review` until the thesis logic or portfolio weights are verified.

HTTP manual run, including a backfill date:

```bash
curl -X POST http://127.0.0.1:8765/api/checks/run \
  -H "X-Signal-Track-Key: $SIGNAL_TRACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"provider":"auto","date":"2026-06-10"}'

curl "http://127.0.0.1:8765/api/projects/1/report?format=markdown"
```

CLI update commands publish automatically when `GO_SITES_DEMO_PUBLISH_URL`,
`GO_SITES_DEMO_API_KEY`, and `SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE=true` are set.
Use `--no-publish` for a one-off local update.

## Backup

SQLite backup:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli backup-db --out "/srv/signal-track/shared/backup-$(date +%F).sqlite3"
```

Verify the live DB and any backup before relying on it:

```bash
/srv/signal-track/venv/bin/python -m signal_track.cli verify-db
/srv/signal-track/venv/bin/python -m signal_track.cli --db "/srv/signal-track/shared/backup-2026-06-06.sqlite3" verify-db
```

Restore only after stopping writers. The command verifies the backup first and
refuses to overwrite the live DB unless `--force` is explicit:

```bash
sudo systemctl stop signal-track.service signal-track-daily.timer
/srv/signal-track/venv/bin/python -m signal_track.cli restore-db --from "/srv/signal-track/shared/backup-2026-06-06.sqlite3" --force
/srv/signal-track/venv/bin/python -m signal_track.cli verify-db
sudo systemctl start signal-track.service signal-track-daily.timer
```

Keep `signal-track.env` outside git and back it up with your normal secret store.

## Upgrade

```bash
cd /srv/signal-track/app
git pull
/srv/signal-track/venv/bin/pip install -e ".[web,market,llm]"
/srv/signal-track/venv/bin/python -m signal_track.cli backup-db --out "/srv/signal-track/shared/pre-upgrade-$(date +%F).sqlite3"
/srv/signal-track/venv/bin/python -m signal_track.cli migrate-db
sudo systemctl restart signal-track.service
```
