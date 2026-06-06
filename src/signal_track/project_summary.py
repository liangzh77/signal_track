from __future__ import annotations

from .analytics import ProjectPerformance
from .db import Repository


def project_summaries(
    repo: Repository,
    project_ids: list[int],
    performances: dict[int, ProjectPerformance] | None = None,
) -> list[dict]:
    return [
        project_summary(row, performance=(performances or {}).get(int(row["id"])))
        for row in repo.list_project_rows_by_ids(project_ids)
    ]


def project_summary(row, performance: ProjectPerformance | None = None) -> dict:
    status = str(row["status"])
    summary = {
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
    if performance:
        summary["performance"] = performance_summary(performance)
    return summary


def performance_summary(performance: ProjectPerformance) -> dict:
    return {
        "return_pct": performance.return_pct,
        "latest_date": performance.latest_date,
        "points": performance.points,
        "point_count": len(performance.points),
        "missing_price_symbols": performance.missing_price_symbols,
        "legs": [
            {
                "symbol": leg.symbol,
                "name": leg.name,
                "direction": leg.direction,
                "weight": leg.weight,
                "return_pct": leg.return_pct,
                "latest_price": leg.latest_price,
                "latest_date": leg.latest_date,
            }
            for leg in performance.legs
        ],
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
