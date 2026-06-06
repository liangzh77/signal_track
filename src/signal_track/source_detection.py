from __future__ import annotations

import re
from dataclasses import dataclass

from .extraction import ExtractedInput


MISSING_SOURCE_NAMES = {"", "manual", "unknown", "none", "null", "未提供", "未知", "手动"}
SOURCE_MARKER_RE = re.compile(r"\s*(?:信息源|来源|source)\s*[:：]\s*(?P<source>.+?)\s*$", flags=re.I)
INLINE_SOURCE_SEPARATORS = ("；", ";", "｜", "|", "，", ",")


@dataclass(frozen=True)
class SourceMarker:
    source: str
    remainder: str = ""


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
        marker = parse_source_marker_line(line)
        if not marker:
            continue
        return marker.source
    return None


def remove_source_marker_lines(content: str) -> str:
    lines = content.splitlines()
    cleaned = []
    for index, line in enumerate(lines):
        marker = parse_source_marker_line(line) if index < 8 else None
        if marker:
            if marker.remainder:
                cleaned.append(marker.remainder)
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


def parse_source_marker_line(line: str) -> SourceMarker | None:
    match = SOURCE_MARKER_RE.match(line)
    if not match:
        return None
    raw_source = match.group("source").strip()
    remainder = ""
    for separator in INLINE_SOURCE_SEPARATORS:
        if separator not in raw_source:
            continue
        raw_source, remainder = raw_source.split(separator, 1)
        raw_source = raw_source.strip()
        remainder = remainder.strip()
        break
    source = normalize_source_name(raw_source)
    if not source:
        return None
    return SourceMarker(source=source, remainder=remainder)
