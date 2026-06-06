from __future__ import annotations

from .db import Repository


def input_summaries(repo: Repository, limit: int = 100) -> list[dict]:
    return [input_summary(row) for row in repo.list_raw_inputs(limit=limit)]


def input_detail(repo: Repository, input_id: int) -> dict | None:
    row = repo.get_raw_input(input_id)
    if not row:
        return None
    detail = input_summary(row)
    detail["content"] = row["content"]
    return detail


def input_summary(row) -> dict:
    content = str(row["content"] or "")
    return {
        "id": int(row["id"]),
        "source_id": int(row["source_id"]),
        "source_name": row["source_name"],
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
