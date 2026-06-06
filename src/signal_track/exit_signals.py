from __future__ import annotations

from .db import Repository
from .project_summary import project_summary


def exit_signal_summaries(repo: Repository, limit: int = 100) -> list[dict]:
    rows = repo.list_project_rows_by_status(["exit_signal"], limit=limit)
    items: list[dict] = []
    for row in rows:
        item = project_summary(row)
        checks = repo.list_daily_checks(project_id=int(row["id"]), limit=1)
        item["latest_check"] = dict(checks[0]) if checks else None
        items.append(item)
    return items
