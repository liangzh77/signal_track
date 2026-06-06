from __future__ import annotations

import json

from .db import Repository


def input_summaries(repo: Repository, limit: int = 100) -> list[dict]:
    return [input_summary(repo, row) for row in repo.list_raw_inputs(limit=limit)]


def input_detail(repo: Repository, input_id: int) -> dict | None:
    row = repo.get_raw_input(input_id)
    if not row:
        return None
    detail = input_summary(repo, row)
    detail["content"] = row["content"]
    return detail


def input_summary(repo: Repository, row) -> dict:
    content = str(row["content"] or "")
    metadata = parse_metadata(row["metadata"])
    projects = input_project_summaries(repo, int(row["id"]), metadata)
    return {
        "id": int(row["id"]),
        "source_id": int(row["source_id"]),
        "source_name": row["source_name"],
        "project_ids": [project["id"] for project in projects],
        "projects": projects,
        "input_action": metadata.get("input_action") or infer_input_action(projects),
        "resolved_symbols": metadata.get("resolved_symbols", []),
        "logic_score": metadata.get("logic_score"),
        "system_logic_added": metadata.get("system_logic_added"),
        "content_preview": compact_preview(content),
        "content_length": len(content),
        "attachment_path": row["attachment_path"],
        "metadata": row["metadata"],
        "received_at": row["received_at"],
    }


def compact_preview(content: str, limit: int = 240) -> str:
    normalized = " ".join(content.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def input_project_summaries(repo: Repository, raw_input_id: int, metadata: dict | None = None) -> list[dict]:
    project_ids = parse_project_ids((metadata or {}).get("project_ids"))
    rows = repo.list_project_rows_by_ids(project_ids) if project_ids else repo.list_project_rows_by_raw_input_id(raw_input_id)
    return [
        {
            "id": int(row["id"]),
            "action": input_project_action(str(row["status"])),
            "title": row["title"],
            "status": row["status"],
            "direction": row["direction"],
            "symbols": split_joined(row["symbols"]),
            "instrument_names": split_joined(row["instrument_names"]),
            "entry_date": row["entry_date"],
            "closed_date": row["closed_date"],
        }
        for row in rows
    ]


def input_project_action(status: str) -> str:
    if status == "closed":
        return "close"
    if status == "exit_signal":
        return "exit_signal"
    return "track"


def split_joined(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def parse_metadata(value: str | None) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_project_ids(value) -> list[int]:
    if not isinstance(value, list):
        return []
    project_ids: list[int] = []
    for item in value:
        try:
            project_id = int(item)
        except (TypeError, ValueError):
            continue
        if project_id > 0 and project_id not in project_ids:
            project_ids.append(project_id)
    return project_ids


def infer_input_action(projects: list[dict]) -> str:
    actions = [str(project.get("action") or "") for project in projects]
    unique = [action for action in dict.fromkeys(actions) if action]
    if not unique:
        return "none"
    if len(unique) == 1:
        return unique[0]
    return "mixed"
