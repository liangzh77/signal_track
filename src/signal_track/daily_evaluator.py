from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .analytics import ProjectPerformance


@dataclass(frozen=True)
class DailyEvaluation:
    conclusion: str
    summary: str
    triggered_rules: list[str] = field(default_factory=list)
    confidence: float = 0.5


class DailyLogicEvaluator:
    def evaluate(
        self,
        *,
        project: Any,
        logic_blocks: list[Any],
        research_items: list[Any],
        performance: ProjectPerformance,
        previous_checks: list[Any],
        check_date: date,
    ) -> DailyEvaluation:
        raise NotImplementedError


DAILY_EVALUATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["conclusion", "summary", "triggered_rules", "confidence"],
    "properties": {
        "conclusion": {"type": "string", "enum": ["hold", "watch", "exit_signal", "needs_review"]},
        "summary": {"type": "string"},
        "triggered_rules": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def build_evaluation_prompt(
    project: Any,
    logic_blocks: list[Any],
    research_items: list[Any],
    performance: ProjectPerformance,
    previous_checks: list[Any],
    check_date: date,
) -> str:
    logic_text = "\n\n".join(
        f"[{block['logic_type']}]\n{block['content']}"
        for block in logic_blocks
    )
    leg_text = "\n".join(
        (
            f"- {leg.symbol} {leg.name}: weight={leg.weight:.2%}, "
            f"latest_price={format_optional(leg.latest_price)}, return={format_return(leg.return_pct)}, "
            f"latest_date={leg.latest_date or 'unknown'}"
        )
        for leg in performance.legs
    )
    checks_text = "\n".join(
        f"- {row['check_date']} {row['conclusion']}: {row['summary']}"
        for row in previous_checks[:5]
    ) or "无"
    research_text = format_research_items(research_items)
    return (
        f"检查日期：{check_date.isoformat()}\n"
        f"项目：{project['title']} / {project['direction']} / {project['status']}\n"
        f"项目收益：{format_return(performance.return_pct)}；最新日期：{performance.latest_date or 'unknown'}\n"
        f"缺失行情：{', '.join(performance.missing_price_symbols) or '无'}\n\n"
        f"标的：\n{leg_text or '无'}\n\n"
        f"逻辑：\n{logic_text or '无'}\n\n"
        f"Research items / pending verification:\n{research_text}\n\n"
        f"最近检查：\n{checks_text}"
    )


def daily_evaluation_from_dict(data: dict) -> DailyEvaluation:
    conclusion = str(data.get("conclusion") or "watch")
    if conclusion not in {"hold", "watch", "exit_signal", "needs_review"}:
        conclusion = "watch"
    return DailyEvaluation(
        conclusion=conclusion,
        summary=str(data.get("summary") or ""),
        triggered_rules=[str(item) for item in data.get("triggered_rules", [])],
        confidence=float(data.get("confidence") or 0.5),
    )


def format_research_items(research_items: list[Any]) -> str:
    if not research_items:
        return "none"
    return "\n".join(
        f"- {row['item_type']} / {row['status']}: {row['content']}"
        for row in research_items[:12]
    )
def format_optional(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.4f}"


def format_return(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.2%}"
