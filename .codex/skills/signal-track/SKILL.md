---
name: signal-track
description: Project-local workflow for Signal Track investment notes. Use in this repository when the user pastes or drops source material, asks to ingest investment signals, update tracking projects, run daily checks, supplement weak trading logic with 3C-5M-3D-3T research, render the dashboard, or publish the static Signal Track page.
---

# Signal Track

## Overview

Signal Track is Codex-first. Codex does the AI work inside the Windows Codex App; this repo provides local deterministic tools for SQLite state, market data, static dashboard rendering, and publishing through the external demo publish API.

Do not start or design a backend service for Signal Track. Do not call an LLM from repo code. The published page is static HTML for display only.

Use SQLite as the runtime source of truth. Use Markdown for human-readable long-form research reports, source archives, and reviews. Do not rely on reparsing Markdown to infer active project state.

## Ingest Workflow

1. Confirm the information source if the user did not provide one.
2. Read pasted text or dropped files and preserve the original note text.
3. Extract instruments, action, direction, source logic, observation logic, portfolio status, and weights.
4. If multiple instruments are present, create separate projects unless the user explicitly says they are a portfolio.
5. If portfolio weights are missing, ask for weights before saving.
6. If logic is weak, still track it and supplement with the user's 3C-5M-3D-3T framework.
7. Write structured extraction JSON and run local CLI ingest, usually with `--archive-reports`.
8. Render and publish the dashboard when configured or requested.

Read `references/tracking-workflow.md` when you need the exact structured JSON shape.

## Daily Check Workflow

Use Codex App Automations for recurring runs. The standard command is:

```powershell
python -m signal_track.cli daily-run --provider auto --archive-reports --publish
```

If market data is unavailable, degrade to:

```powershell
python -m signal_track.cli daily-run --provider none --archive-reports --publish
```

After each run, summarize checked projects, exit signals, publish status, and manual follow-ups.

If a futures project needs historical prices and the configured provider cannot fetch them, ask for or use a licensed/exported daily-bar CSV and import it with:

```powershell
python -m signal_track.cli import-bars "<symbol or name>" --market CN_FUT --file "<csv path>" --provider licensed-csv
```

## Project Boundaries

- Use `.env` for local secrets and keep it ignored by git.
- Use `.env.example` only for placeholders.
- Keep docs aligned with the no-backend architecture.
- Keep dashboard output responsive for desktop landscape and mobile portrait.
- Use the external demo publish API only as a static HTML upload target.
- Store structured state in SQLite and long-form analysis in Markdown.
