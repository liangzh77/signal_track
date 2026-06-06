# Signal Track

[![CI](https://github.com/liangz77/signal_track/actions/workflows/ci.yml/badge.svg)](https://github.com/liangz77/signal_track/actions/workflows/ci.yml)

Signal Track turns raw investment source notes into monitored tracking projects.

Current implementation status:

- Local configuration via `.env`.
- SQLite schema for instruments, prices, sources, inputs, tracking projects, logic blocks, research items, daily checks, and publish events.
- Instrument resolver for A shares, Hong Kong stocks, China futures, US stocks, and US futures seed symbols.
- Unified daily bar interface with fixture, Tushare, yfinance, and auto-routed providers.
- Heuristic extraction plus optional OpenAI Structured Outputs extraction and low-logic tracking supplement.
- Daily check flow with price refresh, return calculation, exit-signal thresholding, and HTML rendering.
- Glassmorphism dashboard with project list, detail cards, logic blocks, and SVG return curves.
- CLI and FastAPI backend for ingestion, checking, rendering, publishing, and serving.

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
python -m signal_track.cli init-db
python -m signal_track.cli migrate-db
python -m signal_track.cli resolve 宁德时代
python -m signal_track.cli refresh-instruments --provider fixture --market CN_A
python -m signal_track.cli fetch-bars 300750.SZ --provider fixture
python -m signal_track.cli ingest --source 测试源 --text "腾讯 做多，先跟踪。"
python -m signal_track.cli daily-run --provider fixture --out dist/dashboard.html
python -m signal_track.cli self-check --out dist/self-check.html
```

By default the local database is `data/signal_track.sqlite3`.

## Configuration

Copy `.env.example` to `.env` and fill provider credentials:

```text
SIGNAL_TRACK_DB_PATH=data/signal_track.sqlite3
SIGNAL_TRACK_ENABLE_SCHEDULER=false
SIGNAL_TRACK_DAILY_PROVIDER=auto
SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE=true
SIGNAL_TRACK_API_KEY=<your-signal-track-api-key>
TUSHARE_TOKEN=<your-tushare-token>
OPENAI_API_KEY=<your-openai-api-key>
SIGNAL_TRACK_OPENAI_MODEL=gpt-4o-mini
GO_SITES_DEMO_PUBLISH_URL=https://liangz77.cn/api/demos/publish
GO_SITES_DEMO_API_KEY=<your-demo-api-key>
```

`.env` is intentionally ignored by git.

## Backend Service

Install web extras and run the FastAPI backend:

```powershell
pip install -e .[web]
python -m signal_track.cli serve --host 127.0.0.1 --port 8000
```

Set `SIGNAL_TRACK_ENABLE_SCHEDULER=true` to run the daily 19:00 Asia/Shanghai
job inside the backend process. `SIGNAL_TRACK_DAILY_PROVIDER` controls the provider
used by that job.

Useful endpoints:

- `GET /health`
- `GET /api/market-data/coverage?provider=auto`
- `GET /api/inputs`
- `GET /api/inputs/{input_id}`
- `POST /api/inputs` with `{ "source": "...", "content": "...", "portfolio": false }`
- `POST /api/inputs/file` multipart upload with `file`, `source`, `portfolio`, `extractor`
- `GET /api/instruments`
- `POST /api/instruments/refresh` with `{ "provider": "auto", "market": "CN_A" }`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `POST /api/projects/{project_id}/close` with `{ "closed_date": "2026-06-10", "reason": "..." }`
- `PATCH /api/projects/{project_id}/weights` with `{ "weights": { "300750.SZ": 60, "600519.SH": 40 } }`
- `GET /api/research-items`
- `PATCH /api/research-items/{item_id}` with `{ "status": "verified" }`
- `POST /api/checks/run` with optional `{ "provider": "auto" }`
- `GET /dashboard`
- `POST /api/publish`
- `GET /api/publish/events`

When publish credentials are configured, `POST /api/inputs`, `POST /api/projects/{project_id}/close`,
research item updates, and `POST /api/checks/run` automatically publish the refreshed dashboard.
CLI update commands do the same by default when `SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE=true`;
use `--no-publish` for a one-off local update.
Publish responses include `url` when the publish API returns a public dashboard
address, plus `publish_url` for the API endpoint that was called.

Ingestion responses include a `projects` summary so the caller can immediately
see whether the input created a tracking item or closed one, plus each project's
status, direction, symbols, source, logic score, and review flags.

Inputs require a real source name. Pass `source`, or put a marker in the first
few lines of the note, for example `source: Alpha Desk` or `信息源：Alpha Desk`.
If no source can be determined, ingestion returns `source_required` and does not
create a tracking project.

If `SIGNAL_TRACK_API_KEY` is configured, mutating endpoints require either:

```text
Authorization: Bearer <your-signal-track-api-key>
X-Signal-Track-Key: <your-signal-track-api-key>
```

Example:

```bash
curl -X POST http://127.0.0.1:8765/api/inputs \
  -H "X-Signal-Track-Key: $SIGNAL_TRACK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"source":"信息源A","content":"腾讯 做多，先跟踪。"}'
```

## Production Deployment

Linux/systemd deployment templates live under `deploy/`, with an operational runbook at
[docs/RUNBOOK.md](docs/RUNBOOK.md).

Basic flow:

```bash
git clone https://github.com/liangzh77/signal_track /srv/signal-track/app
cd /srv/signal-track/app
sudo bash scripts/install_service.sh
sudo editor /srv/signal-track/shared/signal-track.env
sudo systemctl start signal-track.service
sudo systemctl start signal-track-daily.timer
```

Health check:

```bash
python scripts/healthcheck.py http://127.0.0.1:8765/health
```

## Daily Job

Run the full daily flow locally:

```powershell
python -m signal_track.cli daily-run --provider auto
```

Run a non-destructive smoke check with a temporary database:

```powershell
python -m signal_track.cli self-check --provider fixture --out dist/self-check.html
```

For development without provider credentials:

```powershell
python -m signal_track.cli daily-run --provider fixture --out dist/dashboard.html
```

The flow is intentionally sequential:

1. Refresh missing price data when a provider is selected.
2. Run checks for active projects.
3. Render the dashboard HTML.
4. Publish through the demo API when publish credentials are configured and
   `SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE=true`, or when `--publish` is passed.

Closed projects keep refreshing prices for 31 days after `closed_date` so charts
can show the requested post-close window without creating new daily check rows.

## Automatic Check Rules

Daily checks currently execute deterministic price rules found in the stored logic:

- `跌破 N 日线`: triggers an exit signal when the latest close is below the N-day moving average.
- `回撤/亏损/跌幅/止损 N%`: triggers an exit signal when project return is at or below `-N%`.
- `止盈/涨幅/收益/盈利 N%`: triggers an exit signal when project return is at or above `N%`.

Non-price rules such as margin, revenue, orders, industry prices, or management changes
are saved in the source/system logic blocks and marked for future data-provider or LLM
review. They are not silently guessed.

When `OPENAI_API_KEY` is configured, daily checks also run a structured logic
evaluation over the source logic, system supplement, current performance, and recent
check history. The evaluator can mark `hold`, `watch`, `needs_review`, or
`exit_signal`; deterministic price exits still take priority.

## Tests

```powershell
$env:PYTHONPATH='src'
python -m unittest discover -s tests
```

## Database Maintenance

Apply schema migrations:

```powershell
python -m signal_track.cli migrate-db
```

Create a SQLite-safe backup, including WAL state:

```powershell
python -m signal_track.cli backup-db --out data\backup.sqlite3
```

## Market Data Providers

- `fixture`: deterministic local bars for tests and UI development.
- `auto`: routes by market. Tushare handles A shares, Hong Kong stocks, China futures, and US stocks when `TUSHARE_TOKEN` is configured; yfinance handles Hong Kong stocks, US stocks, and US futures when installed.
- `tushare`: A shares, Hong Kong stocks, China futures, and US stocks when `TUSHARE_TOKEN` is configured.
- `yfinance`: temporary fallback for US stocks, Hong Kong stocks, and US futures.

US futures support is intentionally provider-abstracted. For production-grade historical futures data, wire the same interface to CME DataMine or another licensed futures source.
yfinance parsing accepts both ordinary columns and MultiIndex columns returned by
newer yfinance versions.

Inspect current provider coverage without calling remote market APIs:

```powershell
python -m signal_track.cli market-coverage --provider auto
```

The report shows, per market, the configured daily-price provider, whether real
instrument-master refresh is available, and which dependency or credential is
missing when a market is not price-ready.

## Instrument Master

Refresh all supported instrument master records using the auto router:

```powershell
python -m signal_track.cli refresh-instruments --provider auto --market all
```

Refresh one market:

```powershell
python -m signal_track.cli refresh-instruments --provider tushare --market CN_A
python -m signal_track.cli refresh-instruments --provider tushare --market HK
python -m signal_track.cli refresh-instruments --provider tushare --market CN_FUT
python -m signal_track.cli refresh-instruments --provider tushare --market US
```

Without provider credentials, use the fixture provider to seed representative symbols:

```powershell
python -m signal_track.cli refresh-instruments --provider fixture --market all
```

## Signal Extraction

Default extraction is `auto`: it uses structured OpenAI extraction when
`OPENAI_API_KEY` is configured, and falls back to local heuristic extraction
without network access:

```powershell
python -m signal_track.cli ingest --source 信息源A --text "腾讯 做多，先跟踪。"
```

You can also ingest a text or markdown file:

```powershell
python -m signal_track.cli ingest --source 信息源A --file .\notes\source-note.md
```

List recent raw inputs and uploaded attachment paths:

```powershell
python -m signal_track.cli list-inputs --limit 20
python -m signal_track.cli show-input 1
```

If `--source` is omitted, the first few lines of the note must include a marker
such as `source: Alpha Desk`, `来源：Alpha Desk`, or `信息源：Alpha Desk`.
Otherwise the CLI returns `source_required` and skips ingestion.

For portfolio notes, either pass `--portfolio` or write an explicit portfolio
marker in the note, such as `组合`, `portfolio`, `权重`, or `占比`. If the note
includes weights such as `宁德时代 60%，贵州茅台 40%`, Signal Track applies them
automatically. If no weights are found, it creates an equal-weight project and
marks the weight for review. Plain multi-instrument notes without portfolio
markers still split into separate tracking projects.

If a later input contains close words such as `平仓`, `止盈`, `止损`, `退出`, or
`exit`, Signal Track first looks for active projects from the same source that
contain the resolved instrument and closes those projects instead of creating
duplicates. Other sources tracking the same symbol remain independent:

```powershell
python -m signal_track.cli ingest --source 信息源A --text "腾讯 平仓，游戏复苏低于预期。"
```

If the same source sends a non-close follow-up for the same active instrument and
direction, Signal Track appends a `source_update` logic block to the existing
project instead of creating a duplicate tracking item.

Force structured model extraction with `--extractor openai`, or force the local
offline parser with `--extractor heuristic`:

```powershell
python -m signal_track.cli ingest --extractor openai --source 信息源A --text "腾讯 做多，先跟踪。"
python -m signal_track.cli ingest --extractor heuristic --source 信息源A --text "腾讯 做多，先跟踪。"
```

The OpenAI path uses Structured Outputs with a JSON Schema so the system can receive
multiple signals, open/close actions, portfolio flags, directions, weights,
source logic, observation logic, and logic scores in a predictable shape. Close
actions update matching active projects instead of creating duplicate tracking
items. When the raw source logic is weak, Signal Track still creates the tracking
project and stores a system-supplemented 3C-5M-3D-3T logic block.

When `OPENAI_API_KEY` is configured, weak-logic projects also get a structured
tracking supplement with concrete metrics, exit/review conditions, and data
verification notes. These are saved as `research_items` so the dashboard and
project API can expose pending metrics, exit conditions, and unverified data
requirements. Without an API key, the local 3C-5M-3D-3T fallback still creates
a research checklist covering financial/valuation cross-checks, industry and
competition review, latest company dynamics, daily price/sentiment metrics, and
exit-review conditions.

Research item statuses can be maintained manually while the research automation is
being expanded:

```powershell
python -m signal_track.cli list-research-items --project-id 1
python -m signal_track.cli update-research-item 1 --status verified --source-note "checked filing"
python -m signal_track.cli update-research-item 1 --status contradicted --check --provider auto --publish
python -m signal_track.cli update-project-weights 1 --weights-json '{"300750.SZ":60,"600519.SH":40}'
python -m signal_track.cli close-project 1 --date 2026-06-10 --reason "manual exit after thesis broke" --publish
```

When publish credentials are configured, research item updates publish the
refreshed dashboard automatically unless `--no-publish` is passed. Pass
`run_check: true` in the API, or `--check` in the CLI, to recalculate active
project status immediately after a research item update.
