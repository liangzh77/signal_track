from __future__ import annotations

import json
import math
from datetime import date

from .db import Repository

ALLOWED_MANUAL_LOGIC_TYPES = {"source_update", "system_logic", "manual_note"}


class ProjectActionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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


def update_tracking_project_weights(
    repo: Repository,
    project_id: int,
    weights: dict[str, float],
    note: str | None = None,
) -> dict | None:
    project = repo.get_project_row(project_id)
    if not project:
        return None
    legs = repo.list_project_legs(project_id)
    if not legs:
        raise ProjectActionError("project_has_no_legs", "Project has no legs")
    if not weights:
        raise ProjectActionError("weights_required", "At least one weight is required")

    by_key: dict[str, object] = {}
    for leg in legs:
        keys = [
            leg["symbol"],
            leg["provider_symbol"],
            leg["name"],
            *split_aliases(leg["aliases"]),
        ]
        for key in keys:
            if key:
                by_key[str(key).strip().lower()] = leg

    matched: dict[int, float] = {}
    unknown: list[str] = []
    for raw_key, raw_weight in weights.items():
        key = str(raw_key).strip().lower()
        leg = by_key.get(key)
        if not leg:
            unknown.append(str(raw_key))
            continue
        try:
            weight = normalize_input_weight(raw_weight)
        except (TypeError, ValueError) as exc:
            raise ProjectActionError("invalid_weight", f"Weight for {raw_key} must be a finite number") from exc
        if weight <= 0:
            raise ProjectActionError("invalid_weight", f"Weight for {raw_key} must be positive")
        matched[int(leg["id"])] = weight

    if unknown:
        raise ProjectActionError("unknown_weight_symbol", "Unknown weight symbols: " + ", ".join(unknown))
    if len(matched) != len(legs):
        missing = [
            str(leg["symbol"])
            for leg in legs
            if int(leg["id"]) not in matched
        ]
        raise ProjectActionError("incomplete_weights", "Missing weights for: " + ", ".join(missing))

    normalized = normalize_weight_values(matched)
    repo.update_project_leg_weights(project_id, normalized)
    repo.add_logic_block(
        project_id,
        "weight_update",
        note or "Manual portfolio weight update",
        1.0,
        [f"{leg_id}: {weight:.6f}" for leg_id, weight in sorted(normalized.items())],
    )
    updated = repo.get_project_row(project_id)
    return dict(updated) if updated else None


def add_project_logic_block(
    repo: Repository,
    project_id: int,
    content: str,
    logic_type: str = "source_update",
    confidence: float = 1.0,
    evidence: list[str] | None = None,
) -> dict | None:
    project = repo.get_project_row(project_id)
    if not project:
        return None
    clean_type = (logic_type or "source_update").strip()
    if clean_type not in ALLOWED_MANUAL_LOGIC_TYPES:
        raise ProjectActionError(
            "invalid_logic_type",
            "logic_type must be one of: " + ", ".join(sorted(ALLOWED_MANUAL_LOGIC_TYPES)),
        )
    clean_content = content.strip()
    if not clean_content:
        raise ProjectActionError("logic_content_required", "Logic content is required")
    try:
        clean_confidence = float(confidence)
    except (TypeError, ValueError) as exc:
        raise ProjectActionError("invalid_confidence", "confidence must be a finite number between 0 and 1") from exc
    if not math.isfinite(clean_confidence) or clean_confidence < 0 or clean_confidence > 1:
        raise ProjectActionError("invalid_confidence", "confidence must be between 0 and 1")
    repo.add_logic_block(project_id, clean_type, clean_content, clean_confidence, evidence or [clean_content[:240]])
    updated = repo.get_project_row(project_id)
    return dict(updated) if updated else None


def split_aliases(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        aliases = json.loads(value)
        if isinstance(aliases, list):
            return [str(item).strip() for item in aliases if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [item.strip() for item in str(value).split(",") if item.strip()]


def normalize_input_weight(value: float) -> float:
    weight = float(value)
    if not math.isfinite(weight):
        raise ValueError("weight must be finite")
    if weight > 1:
        weight = weight / 100
    return weight


def normalize_weight_values(weights: dict[int, float]) -> dict[int, float]:
    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        raise ProjectActionError("invalid_weight_total", "Weight total must be positive")
    return {leg_id: weight / total for leg_id, weight in weights.items()}
