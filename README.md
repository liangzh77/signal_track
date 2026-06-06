# Signal Track

[![CI](https://github.com/liangz77/signal_track/actions/workflows/ci.yml/badge.svg)](https://github.com/liangz77/signal_track/actions/workflows/ci.yml)

Signal Track turns raw investment source notes into monitored tracking projects.

Current implementation status:

- Local configuration via `.env`.
- SQLite schema for instruments, prices, sources, inputs, tracking projects, logic blocks, research items, daily checks, and publish events.
- Instrument resolver for A shares, Hong Kong stocks, China futures, Hong Kong futures, US stocks, and US futures seed symbols.
- Unified daily bar interface with fixture, Tushare, yfinance, and auto-routed providers.
- Heuristic extraction plus optional OpenAI Structured Outputs extraction and low-logic tracking supplement.
- Daily check flow with price refresh, return calculation, exit-signal thresholding, and HTML rendering.
- Glassmorphism dashboard with source/status/direction filters, project list, detail cards, embedded report snapshots, logic blocks, and SVG return curves.
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
SIGNAL_TRACK_OPENAI_WEB_RESEARCH=false
SIGNAL_TRACK_OPENAI_WEB_SEARCH_CONTEXT_SIZE=medium
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

Set `SIGNAL_TRACK_ENABLE_SCHEDULER=true` to run scheduled checks inside the
backend process. The built-in scheduler runs at 19:00 Asia/Shanghai for A shares,
Hong Kong, and China futures, plus 07:00 Asia/Shanghai as a US-market catch-up
pass. `SIGNAL_TRACK_DAILY_PROVIDER` controls the provider used by those jobs and
defaults to `auto`; set it to `none` only when you want checks to evaluate
already-stored prices without refreshing market data. `/health` includes
`scheduler_jobs` so deployments can verify that the 19:00 and 07:00 jobs are
registered.

Useful endpoints:

- `GET /health`
- `GET /api/market-data/coverage?provider=auto`
- `GET /api/market-data/smoke?provider=fixture&market=US_FUT&days=30`
- `GET /api/inputs`
- `GET /api/inputs/{input_id}`
- `POST /api/inputs` with `{ "source": "...", "content": "...", "portfolio": false, "extractor": "auto" }`
- `POST /api/inputs/file` multipart upload with `file`, `source`, `portfolio`, `extractor`
- `GET /api/instruments`
- `POST /api/instruments/refresh` with `{ "provider": "auto", "market": "CN_A" }`
- `GET /api/projects?source=Alpha%20Desk&status=needs_review&direction=long`
- `GET /api/exit-signals`
- `GET /api/projects/{project_id}`
- `GET /api/projects/{project_id}/report?format=markdown` or `format=json`
- `POST /api/projects/{project_id}/close` with `{ "closed_date": "2026-06-10", "reason": "..." }`
- `PATCH /api/projects/{project_id}/weights` with `{ "weights": { "300750.SZ": 60, "600519.SH": 40 } }`
- `GET /api/research-items`
- `PATCH /api/research-items/{item_id}` with `{ "status": "verified" }`
- `POST /api/checks/run` with optional `{ "provider": "auto", "date": "2026-06-10" }`; if provider is omitted, it uses `SIGNAL_TRACK_DAILY_PROVIDER`
- `GET /dashboard`
- `POST /api/publish`
- `GET /api/publish/events`

When publish credentials are configured, `POST /api/inputs`, `POST /api/projects/{project_id}/close`,
research item updates, and `POST /api/checks/run` automatically publish the refreshed dashboard.
This automatic publish behavior is controlled by `SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE`
and is enabled by default. CLI update commands also accept `--no-publish` for a
one-off local update; manual `POST /api/publish` is still available when automatic
publishing is disabled.
Publish responses include `url` when the publish API returns a public dashboard
address, plus `publish_url` for the API endpoint that was called. Failed publish
attempts keep the data update, record a publish event, and return `ok: false`
with `error` and `response_body` so the caller can alert or retry.

Ingestion responses include a `projects` summary so the caller can immediately
see whether the input created a tracking item or closed one, plus each project's
status, direction, symbols, source, logic score, and review flags.
`GET /api/projects` returns the same normalized project summary plus current
performance, curve points, missing price symbols, and leg-level return snapshots;
performance also includes `window_start` and `window_end` so callers know the
chart coverage range. Use `source`, `status`, and `direction` query parameters
to drive filtered project lists.
Project summaries also include `latest_check` and `next_action` so callers can
surface the current decision without fetching full project details.
`GET /api/projects/{project_id}` includes the same normalized summary under
`summary`, plus legs, logic blocks, research items, checks, and full performance.
`GET /api/projects/{project_id}/report` exports a Markdown or JSON project
research report assembled from source logic, system-supplemented
3C-5M-3D-3T tracking logic, research verification items, latest checks, and
price performance. The Markdown report follows the eight-part research structure:
opening judgment, 3C, 5M, 3D, 3T, Fenghe-style perspective, scoring card, and
data sources/disclaimer. It marks unverified data as review material rather than
confirmed facts. The published dashboard also embeds the Markdown report body in
each project detail card so the static uploaded page remains usable without a
live backend.
`GET /api/exit-signals` and `list-exit-signals` use the same performance-bearing
summary and include the latest check that triggered the signal.
`extractor` accepts `auto`, `heuristic`, or `openai`; unknown values return `400`
instead of silently changing extraction behavior.
Provider fields accept `none`, `auto`, `fixture`, `tushare`, or `yfinance`.
Unknown provider names return `400`; missing credentials or dependencies return
`503`.

Inputs require a real source name. Pass `source`, or put a marker in the first
few lines of the note, for example `source: Alpha Desk` or `信息源：Alpha Desk`.
If no source can be determined, ingestion returns `source_required` and does not
create a tracking project.
If you put the source and note on one line, separate them with `;`, `；`, or `|`,
for example `信息源：Alpha Desk；00700.HK 做多，观察广告`.

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
python -m signal_track.cli daily-run
```

`daily-run` and `check` default to `SIGNAL_TRACK_DAILY_PROVIDER` (`auto` by
default). Pass `--provider none` only for an offline rules-only check.

Run a non-destructive smoke check with a temporary database:

```powershell
python -m signal_track.cli self-check --provider fixture --out dist/self-check.html
```

`self-check` covers source-required validation, single-project ingestion,
low-logic system supplement, multi-instrument splitting, portfolio handling,
fixture daily checks, and dashboard rendering without touching the configured
database.

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

If one instrument's price refresh fails, the daily run continues for the other
projects and records the failed symbol as a `needs_review` rule in that project's
daily check.
When a later check has prices again and no review/exit rule is triggered, Signal
Track clears transient `needs_review` status. Low-logic projects and portfolios
with unconfirmed weights remain marked for review until their project-level issue
is resolved.

Closed projects keep refreshing prices for 31 days after `closed_date` so charts
can show the requested post-close window without creating new daily check rows.

## Automatic Check Rules

Daily checks currently execute deterministic price rules found in the stored logic:

- `跌破 N 日线` or `breaks below N day moving average` / `MA N`: triggers an exit signal when the latest close is below the N-day moving average.
- `回撤/亏损/跌幅/止损 N%` or `drawdown/stop loss/loss/downside N%`: triggers an exit signal when project return is at or below `-N%`.
- `止盈/涨幅/收益/盈利 N%` or `take profit/gain/upside/return N%`: triggers an exit signal when project return is at or above `N%`.

Non-price rules such as margin, revenue, orders, industry prices, or management changes
are saved in the source/system logic blocks and marked for future data-provider or LLM
review. They are not silently guessed.

When `OPENAI_API_KEY` is configured, daily checks also run a structured logic
evaluation over the source logic, system supplement, current performance, and recent
check history. The evaluator can mark `hold`, `watch`, `needs_review`, or
`exit_signal`; deterministic price exits still take priority.
Set `SIGNAL_TRACK_OPENAI_WEB_RESEARCH=true` to let OpenAI logic checks force the
Responses API `web_search` tool for up-to-date financial, industry, and news
evidence. Use a web-search-capable model when this is enabled.
`SIGNAL_TRACK_OPENAI_WEB_SEARCH_CONTEXT_SIZE` accepts `low`, `medium`, or `high`.

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
- `auto`: routes by market. Tushare handles A shares, Hong Kong stocks, China futures, and US stocks when `TUSHARE_TOKEN` is configured; yfinance handles Hong Kong stocks, Hong Kong futures, US stocks, and US futures when installed.
- `tushare`: A shares, Hong Kong stocks, China futures, and US stocks when `TUSHARE_TOKEN` is configured.
- `yfinance`: temporary fallback for US stocks, Hong Kong stocks, Hong Kong futures, and US futures.

Hong Kong and US futures support is intentionally provider-abstracted. For production-grade historical futures data, wire the same interface to HKEX Data Services, CME DataMine, or another licensed futures source.
yfinance parsing accepts both ordinary columns and MultiIndex columns returned by
newer yfinance versions.

Inspect current provider coverage without calling remote market APIs:

```powershell
python -m signal_track.cli market-coverage --provider auto
```

The report shows, per market, the configured daily-price provider, whether real
instrument-master refresh is available, and which dependency or credential is
missing when a market is not price-ready.

After credentials and dependencies are configured, run a real sample fetch before
trusting scheduled checks:

```powershell
python -m signal_track.cli market-smoke --provider auto --market all --days 30
```

The smoke check fetches representative daily bars for each selected market and
returns `ok`, `bar_count`, `latest_date`, and any provider error per sample. Use
`--provider fixture` for an offline wiring check.

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
without network access. If the OpenAI package or request fails in `auto` mode,
the note is still processed by the heuristic extractor. Use `--extractor openai`
only when you want ingestion to fail instead of falling back:

```powershell
python -m signal_track.cli ingest --source 信息源A --text "腾讯 做多，先跟踪。"
```

You can also ingest a text or markdown file:

```powershell
python -m signal_track.cli ingest --source 信息源A --file .\notes\source-note.md
```

File ingestion supports text-like files such as `.txt`, `.md`, `.csv`, `.tsv`,
and `.html`, with UTF-8/UTF-16/GB18030 decoding. Modern Word `.docx` files are
parsed for document text. PDF text extraction is available when installed with
`pip install -e .[files]`; otherwise PDFs return `unsupported_input_file`.
Legacy `.doc`, Excel, PowerPoint, images, and zip archives are rejected; convert
those documents to text before ingestion.

List recent raw inputs and uploaded attachment paths:

```powershell
python -m signal_track.cli list-inputs --limit 20
python -m signal_track.cli show-input 1
python -m signal_track.cli list-projects --source 信息源A --status needs_review --direction long
```

If `--source` is omitted, the first few lines of the note must include a marker
such as `source: Alpha Desk`, `来源：Alpha Desk`, or `信息源：Alpha Desk`.
Otherwise the CLI returns `source_required` and skips ingestion.
Inline markers are also supported when separated from the note body with `;`,
`；`, or `|`, for example `source: Alpha Desk; 00700.HK long`.

For portfolio notes, either pass `--portfolio` or write an explicit portfolio
marker in the note, such as `组合`, `portfolio`, `权重`, or `占比`. If the note
includes weights such as `宁德时代 60%，贵州茅台 40%`, Signal Track applies them
automatically. If no weights, or only partial weights, are found, it creates an
equal-weight project and marks the weight for review. Plain multi-instrument
notes without portfolio markers still split into separate tracking projects.
Percentages are treated as weights only when they appear directly after a leg or
inside an explicit weight context such as `weights 60%, 40%`, so upside/downside
or stop-loss percentages do not become accidental portfolio weights.

Pure background mentions are intentionally not promoted into tracking projects.
For example, a note that only says `00700.HK earnings released` is stored as a
raw input with resolved symbols, but returns empty `project_ids`. Structured
extractor results with `action: "none"` behave the same way. Weak open/tracking
signals still create tracking projects and receive system-supplemented logic.
Conditional exit rules inside an opening note, such as `Exit if price breaks 20
day moving average`, are stored as tracking logic and do not close the project.

If a later input contains close words such as `平仓`, `止盈`, `止损`, `退出`, or
`exit`, Signal Track first looks for active projects from the same source that
contain the resolved instrument and closes those projects instead of creating
duplicates. Other sources tracking the same symbol remain independent. If no
same-source active project matches the close signal, Signal Track records the
raw input but does not create a new tracking project:

```powershell
python -m signal_track.cli ingest --source 信息源A --text "腾讯 平仓，游戏复苏低于预期。"
```

For portfolio projects, a close signal must resolve the full portfolio symbol set
before the portfolio is closed. A close signal for only one leg closes a matching
single-instrument project from the same source, but does not close the whole
portfolio.
Portfolio return curves carry forward each leg's latest available return across
missing trading dates, so mixed-market holidays do not underweight the aggregate
curve.
Dashboard review counts include both thesis review (`needs_review`) and portfolio
weight review (`weight_needs_review`).

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
items. In mixed notes, each structured signal's own `action` is handled
independently, so a close signal for one symbol does not turn other open signals
in the same input into closes. When the raw source logic is weak, Signal Track
still creates the tracking project and stores a system-supplemented 3C-5M-3D-3T
logic block.

When `OPENAI_API_KEY` is configured, weak-logic projects also get a structured
tracking supplement with concrete metrics, exit/review conditions, and data
verification notes. These are saved as `research_items` so the dashboard and
project API can expose pending metrics, exit conditions, and unverified data
requirements. Without an API key, the local 3C-5M-3D-3T fallback still creates
a research checklist covering financial/valuation cross-checks, industry and
competition review, latest company dynamics, daily price/sentiment metrics, and
exit-review conditions.
When `SIGNAL_TRACK_OPENAI_WEB_RESEARCH=true`, this supplement step forces web
search and asks the model to gather current financial/valuation, industry, and
latest-dynamics evidence before writing the tracking logic. Unverified or
single-source facts still remain review items instead of confirmed data.

Research item statuses can be maintained manually while the research automation is
being expanded:

```powershell
python -m signal_track.cli list-projects
python -m signal_track.cli list-research-items --project-id 1
python -m signal_track.cli list-exit-signals
python -m signal_track.cli export-project-report 1 --out reports/project-1.md
python -m signal_track.cli export-project-report 1 --format json
python -m signal_track.cli update-research-item 1 --status verified --source-note "checked filing"
python -m signal_track.cli update-research-item 1 --status contradicted --check --provider auto --publish
python -m signal_track.cli update-project-weights 1 --weights-json '{"300750.SZ":60,"600519.SH":40}'
python -m signal_track.cli close-project 1 --date 2026-06-10 --reason "manual exit after thesis broke" --publish
```

When publish credentials are configured, research item updates publish the
refreshed dashboard automatically unless `--no-publish` is passed. Pass
`run_check: true` in the API, or `--check` in the CLI, to recalculate active
project status immediately after a research item update.
