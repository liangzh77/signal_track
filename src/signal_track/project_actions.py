from __future__ import annotations

from datetime import date

from .db import Repository


def close_tracking_project(
    repo: Repository,
    project_id: int,
    closed_date: str | None = None,
    reason: str | None = None,
) -> dict | None:
    project = repo.get_project_row(project_id)
    if not project:
        return None
    close_date = closed_date or date.today().isoformat()
    close_reason = reason or "manual close"
    repo.close_project(
        project_id,
        close_date,
        metadata={
            "close_reason": close_reason,
            "closed_by_signal": False,
        },
    )
    repo.add_logic_block(
        project_id,
        "close_logic",
        close_reason,
        1.0,
        [f"manual_close_date: {close_date}", close_reason],
    )
    updated = repo.get_project_row(project_id)
    return dict(updated) if updated else None
