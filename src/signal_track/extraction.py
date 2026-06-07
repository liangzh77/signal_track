from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExtractedSignal:
    instruments: list[str]
    direction: str
    source_logic: str
    observation_logic: str
    logic_score: float
    action: str = "open"
    is_portfolio: bool = False
    weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExtractedInput:
    signals: list[ExtractedSignal]
    source_name: str | None = None
    needs_review: bool = False
    notes: str = ""


SIGNAL_EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["signals", "source_name", "needs_review", "notes"],
    "properties": {
        "source_name": {"type": ["string", "null"]},
        "needs_review": {"type": "boolean"},
        "notes": {"type": "string"},
        "signals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "instruments",
                    "action",
                    "direction",
                    "source_logic",
                    "observation_logic",
                    "logic_score",
                    "is_portfolio",
                    "weights",
                ],
                "properties": {
                    "instruments": {"type": "array", "items": {"type": "string"}},
                    "action": {"type": "string", "enum": ["open", "close", "none"]},
                    "direction": {"type": "string", "enum": ["long", "short", "neutral"]},
                    "source_logic": {"type": "string"},
                    "observation_logic": {"type": "string"},
                    "logic_score": {"type": "number", "minimum": 0, "maximum": 10},
                    "is_portfolio": {"type": "boolean"},
                    "weights": {
                        "type": "object",
                        "additionalProperties": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
        },
    },
}


def extracted_input_from_dict(data: dict) -> ExtractedInput:
    return ExtractedInput(
        source_name=data.get("source_name"),
        needs_review=bool(data.get("needs_review", False)),
        notes=str(data.get("notes") or ""),
        signals=[
            ExtractedSignal(
                instruments=[str(item) for item in signal.get("instruments", [])],
                action=str(signal.get("action") or "open"),
                direction=str(signal.get("direction") or "neutral"),
                source_logic=str(signal.get("source_logic") or ""),
                observation_logic=str(signal.get("observation_logic") or ""),
                logic_score=float(signal.get("logic_score") or 0),
                is_portfolio=bool(signal.get("is_portfolio", False)),
                weights={str(key): float(value) for key, value in signal.get("weights", {}).items()},
            )
            for signal in data.get("signals", [])
        ],
    )
