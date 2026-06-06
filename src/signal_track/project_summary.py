from __future__ import annotations

from .analytics import ProjectPerformance
from .db import Repository


def project_summaries(
    repo: Repository,
    project_ids: list[int],
    performances: dict[int, ProjectPerformance] | None = None,
    include_latest_check: bool = False,
) -> list[dict]:
    summaries = []
    for row in repo.list_project_rows_by_ids(project_ids):
        project_id = int(row["id"])
        checks = repo.list_daily_checks(project_id=project_id, limit=1) if include_latest_check else []
        latest_check = checks[0] if checks else None
        summaries.append(
            project_summary(
                row,
                performance=(performances or {}).get(project_id),
                latest_check=latest_check,
            )
        )
    return summaries


def project_summary(row, performance: ProjectPerformance | None = None, latest_check=None) -> dict:
    status = str(row["status"])
    summary = {
        "id": int(row["id"]),
        "action": action_for_status(status),
        "next_action": next_action_for_status(status, bool(row["needs_review"]), bool(row["weight_needs_review"])),
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
    if latest_check:
        summary["latest_check"] = {
            "check_date": latest_check["check_date"],
            "conclusion": latest_check["conclusion"],
            "summary": latest_check["summary"],
            "triggered_rules": latest_check["triggered_rules"],
        }
    else:
        summary["latest_check"] = None
    return summary


def performance_summary(performance: ProjectPerformance) -> dict:
    return {
        "return_pct": performance.return_pct,
        "latest_date": performance.latest_date,
        "points": performance.points,
        "point_count": len(performance.points),
        "window_start": performance.window_start,
        "window_end": performance.window_end,
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


def next_action_for_status(status: str, needs_review: bool = False, weight_needs_review: bool = False) -> str:
    if status == "exit_signal":
        return "review_exit"
    if status == "closed":
        return "monitor_post_close"
    if weight_needs_review:
        return "confirm_weights"
    if status == "needs_review" or needs_review:
        return "review_logic"
    return "keep_tracking"


def split_joined(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
