# Signal Track Workflow Reference

## Structured Extraction JSON

When Codex has analyzed a user note, write a temporary JSON file and pass it to:

```powershell
python -m signal_track.cli ingest --source "<source>" --text "<raw note>" --extraction-json "<path>" --publish
```

Shape:

```json
{
  "source_name": "Source Name",
  "needs_review": false,
  "notes": "short extraction note",
  "signals": [
    {
      "instruments": ["00700.HK"],
      "action": "open",
      "direction": "long",
      "source_logic": "original source thesis",
      "observation_logic": "track ads recovery; review if price breaks MA20",
      "logic_score": 8,
      "is_portfolio": false,
      "weights": {}
    }
  ]
}
```

Fields:

- `action`: `open`, `close`, or `none`. Use `open` for new tracking and updates to existing open projects; the CLI will attach updates when an active matching project already exists.
- `direction`: `long`, `short`, `neutral`, or `unknown`.
- `instruments`: ticker symbols or names. Prefer canonical tickers when known.
- `is_portfolio`: true only when the user explicitly says the instruments form a portfolio/combination.
- `weights`: required for portfolio projects. Values may be percentages or fractions.
- `logic_score`: 1-10, where low score means Codex must supplement logic before saving.

## Low-Logic Handling

Do not ask whether to track when logic is weak. Track it and add system logic using the user's 3C-5M-3D-3T framework before saving.

Ask only when:

- The source name is missing.
- Portfolio weights are missing.
- The target instrument cannot be reasonably resolved.

## After Updates

After every ingest, close, note, weight update, or daily check:

1. Render the dashboard.
2. Publish when configured or explicitly requested.
3. Summarize created/updated projects, exit signals, and publish status.
