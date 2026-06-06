from __future__ import annotations

from .analytics import project_performance
from .db import Repository
from .project_summary import project_summary


def exit_signal_summaries(repo: Repository, limit: int = 100) -> list[dict]:
    rows = repo.list_project_rows_by_status(["exit_signal"], limit=limit)
    items: list[dict] = []
    for row in rows:
        item = project_summary(row, performance=project_performance(repo, int(row["id"])))
        checks = repo.list_daily_checks(project_id=int(row["id"]), limit=1)
        item["latest_check"] = dict(checks[0]) if checks else None
        items.append(item)
    return items
