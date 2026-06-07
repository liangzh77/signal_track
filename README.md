# Signal Track

Signal Track is a Codex App-first investment signal tracker.

Here "Codex App-first" means there is no Signal Track backend service. You paste
notes or drag files into the Windows Codex App, Codex analyzes them in the
conversation, and Codex calls the local CLI in this repository to update SQLite
state, refresh prices, render a static HTML dashboard, and publish that HTML
through the external demo publish API.

## Architecture

- Codex App does the AI work: source-note understanding, ticker extraction,
  open/close judgment, portfolio interpretation, and 3C-5M-3D-3T logic
  supplementation.
- The repository provides deterministic local tooling: SQLite persistence,
  instrument resolution, market-data adapters, rule checks, dashboard rendering,
  and publish API calls.
- SQLite is the canonical runtime state. Markdown is used for human-readable
  research reports, source archives, and reviews.
- The published site is a pure static HTML page. It has no backend dependency
  after publishing.
- The demo publish API is only an external HTML upload target. It is not a
  Signal Track analysis backend.
- Daily checks should be scheduled with Codex App Automations, typically at
  19:00 Asia/Shanghai, with an optional 07:00 US-market catch-up.

## What Is Not Included

- No FastAPI backend service.
- No Web Inbox server.
- No backend OpenAI API calls.
- No systemd timer/service deployment.

Backend model calls were intentionally removed. If structured AI judgment is
needed, Codex performs it in the app and passes the result to the CLI as JSON.

## Data Model

Signal Track intentionally uses a hybrid SQLite + Markdown design:

- SQLite stores machine-readable state: inputs, instruments, tracking projects,
  portfolio legs and weights, logic blocks, daily checks, exit signals, price
  bars, `project_reports` artifact indexes, and publish events.
- Markdown stores long-form analysis: project research reports, 3C-5M-3D-3T
  supplements, source archives, and post-trade reviews.
- The dashboard renders from SQLite state plus Markdown summaries or links. It
  should not infer active/closed state by reparsing Markdown files.

See `docs/数据架构.md` for the Chinese design note.
See `docs/完成审计.md` for the current requirement-to-evidence audit.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .[market,files]
python -m signal_track.cli init-db
python -m signal_track.cli migrate-db
python -m signal_track.cli doctor
python -m signal_track.cli self-check --out dist/self-check.html
```

By default the local database is `data/signal_track.sqlite3`.

## Configuration

Copy `.env.example` to `.env` and fill only the providers you actually use:

```text
SIGNAL_TRACK_DB_PATH=data/signal_track.sqlite3
SIGNAL_TRACK_DAILY_PROVIDER=auto
SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE=true
TUSHARE_TOKEN=<your-tushare-token>
GO_SITES_DEMO_PUBLISH_URL=https://liangz77.cn/api/demos/publish
GO_SITES_DEMO_API_KEY=<your-demo-api-key>
```

`.env` is intentionally ignored by git.

## Codex App-First Ingestion

For ordinary use, invoke the repo-local skill at
`.codex/skills/signal-track/SKILL.md`. The skill tells Codex how to:

1. Require an information source before analysis.
2. Extract ticker(s), action, direction, source logic, observation logic, and
   portfolio weights.
3. Supplement weak logic with the 3C-5M-3D-3T research framework inside Codex.
4. Store the result through the local CLI.
5. Render and publish the static dashboard.

The CLI supports a Codex-produced structured extraction file:

```powershell
python -m signal_track.cli ingest `
  --source "Alpha Desk" `
  --text "original source note..." `
  --extraction-json dist/codex-extraction.json `
  --publish
```

If no structured JSON is provided, the CLI uses local heuristic extraction only.

## Daily Automation

Use the Windows Codex App Automation UI for recurring checks. A typical prompt:

```text
In this workspace, run Signal Track daily check and publish:
python -m signal_track.cli daily-run --provider auto --archive-reports --publish
If provider auto is unavailable, run:
python -m signal_track.cli daily-run --provider none --archive-reports --publish
Summarize checked projects, exit signals, publish result, and required manual actions.
```

Suggested schedules:

- 19:00 Asia/Shanghai for A shares, Hong Kong, China futures, and general checks.
- 07:00 Asia/Shanghai optional catch-up for US stocks and US futures.

## Useful CLI Commands

```powershell
python -m signal_track.cli resolve 腾讯
python -m signal_track.cli doctor
python -m signal_track.cli refresh-instruments --provider auto --market all
python -m signal_track.cli market-coverage --provider auto
python -m signal_track.cli market-smoke --provider auto --market all --days 30
python -m signal_track.cli ingest --source 信息源A --text "腾讯 做多，观察广告恢复。" --archive-reports
python -m signal_track.cli check --provider auto --archive-reports --publish
python -m signal_track.cli daily-run --provider auto --out dist/dashboard.html --archive-reports --publish
python -m signal_track.cli render-dashboard --out dist/dashboard.html
python -m signal_track.cli publish-dashboard
python -m signal_track.cli list-projects
python -m signal_track.cli list-exit-signals
python -m signal_track.cli export-project-report 1 --out dist/project-1-report.md
python -m signal_track.cli list-project-reports --project-id 1
```

## Market Data

Provider fields accept `none`, `auto`, `fixture`, `tushare`, or `yfinance`.

- `auto`: routes by market.
- `tushare`: A shares, Hong Kong stocks, China futures, and US stocks when
  `TUSHARE_TOKEN` is configured.
- `yfinance`: fallback for A-share prices, US stocks, Hong Kong stocks, Hong
  Kong futures, and US futures. It does not refresh a full A-share instrument
  master.
- `fixture`: deterministic local test data.

China futures require Tushare or a licensed provider. Hong Kong and US futures
are provider-abstracted through yfinance fallback; for production-grade
historical futures data, wire the same provider interface to a licensed source.

## Dashboard

`render-dashboard` and `daily-run` generate a static HTML dashboard. The published
page must support:

- Desktop landscape layout with dense project tables and detail cards.
- Mobile portrait layout with single-column cards.
- No page-level horizontal overflow on mobile; wide tables may scroll inside
  their table container.
- Project curves from one month before entry to one month after close, or current
  date for active projects.
- Portfolio-level curves plus each leg's separate curve.

The visual direction follows `DESIGN.md`: card-based layered design, futuristic
minimalism, and restrained glassmorphism.

## Testing

```powershell
python -m unittest discover -s tests
python -m compileall src tests
git diff --check
python -m signal_track.cli self-check --provider fixture --out dist/self-check.html
```

## Disclaimer

This project stores and displays investment tracking analysis. It does not place
orders and does not provide investment advice.
