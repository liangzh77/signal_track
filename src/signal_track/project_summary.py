from __future__ import annotations

from .db import Repository


def project_summaries(repo: Repository, project_ids: list[int]) -> list[dict]:
    return [project_summary(row) for row in repo.list_project_rows_by_ids(project_ids)]


def project_summary(row) -> dict:
    status = str(row["status"])
    return {
        "id": int(row["id"]),
        "action": action_for_status(status),
        "title": row["title"],
        "source_name": row["source_name"],
        "status": status,
        "direction": row["direction"],
        "symbols": split_joined(row["symbols"]),
        "instrument_names": split_joined(row["instrument_names"]),
        "logic_score": float(row["logic_score"]),
        "needs_review": bool(row["needs_review"]),
        "weight_needs_review": bool(row["weight_needs_review"]),
        "entry_date": row["entry_date"],
        "closed_date": row["closed_date"],
    }


def action_for_status(status: str) -> str:
    if status == "closed":
        return "close"
    if status == "exit_signal":
        return "exit_signal"
    return "track"


def split_joined(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
