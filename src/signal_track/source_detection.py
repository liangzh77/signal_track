from __future__ import annotations

import re

from .extraction import ExtractedInput


MISSING_SOURCE_NAMES = {"", "manual", "unknown", "none", "null", "未提供", "未知", "手动"}
SOURCE_MARKER_RE = re.compile(r"\s*(?:信息源|来源|source)\s*[:：]\s*(?P<source>.+?)\s*$", flags=re.I)


def resolve_source_name(
    explicit_source: str | None,
    content: str,
    extraction: ExtractedInput | None = None,
) -> str | None:
    source = normalize_source_name(explicit_source)
    if source:
        return source
    extracted = normalize_source_name(extraction.source_name if extraction else None)
    if extracted:
        return extracted
    return infer_source_from_content(content)


def normalize_source_name(value: str | None) -> str | None:
    normalized = (value or "").strip()
    if normalized.lower() in MISSING_SOURCE_NAMES:
        return None
    return normalized


def infer_source_from_content(content: str) -> str | None:
    for line in content.splitlines()[:8]:
        match = SOURCE_MARKER_RE.match(line)
        if not match:
            continue
        source = normalize_source_name(match.group("source"))
        if source:
            return source
    return None


def remove_source_marker_lines(content: str) -> str:
    lines = content.splitlines()
    cleaned = [
        line
        for index, line in enumerate(lines)
        if not (index < 8 and SOURCE_MARKER_RE.match(line))
    ]
    return "\n".join(cleaned).strip()
