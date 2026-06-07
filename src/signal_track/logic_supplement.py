from __future__ import annotations

from dataclasses import dataclass, field

from .models import Direction, Instrument


@dataclass(frozen=True)
class LogicSupplement:
    thesis: str
    tracking_metrics: list[str] = field(default_factory=list)
    exit_conditions: list[str] = field(default_factory=list)
    verification_notes: list[str] = field(default_factory=list)
    confidence: float = 0.5

    def to_block(self) -> str:
        sections = [self.thesis.strip()]
        if self.tracking_metrics:
            sections.append("关键跟踪指标：\n" + "\n".join(f"- {item}" for item in self.tracking_metrics))
        if self.exit_conditions:
            sections.append("平仓/复核触发条件：\n" + "\n".join(f"- {item}" for item in self.exit_conditions))
        if self.verification_notes:
            sections.append("数据验证备注：\n" + "\n".join(f"- {item}" for item in self.verification_notes))
        return "\n\n".join(section for section in sections if section.strip())


class LogicSupplementer:
    def supplement(
        self,
        *,
        name: str,
        direction: Direction,
        source_logic: str,
        instruments: list[Instrument],
    ) -> LogicSupplement:
        raise NotImplementedError


SUPPLEMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["thesis", "tracking_metrics", "exit_conditions", "verification_notes", "confidence"],
    "properties": {
        "thesis": {"type": "string"},
        "tracking_metrics": {"type": "array", "items": {"type": "string"}, "minItems": 3, "maxItems": 8},
        "exit_conditions": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 6},
        "verification_notes": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def logic_supplement_from_dict(data: dict) -> LogicSupplement:
    return LogicSupplement(
        thesis=str(data.get("thesis") or ""),
        tracking_metrics=[str(item) for item in data.get("tracking_metrics", [])],
        exit_conditions=[str(item) for item in data.get("exit_conditions", [])],
        verification_notes=[str(item) for item in data.get("verification_notes", [])],
        confidence=float(data.get("confidence") or 0.5),
    )
