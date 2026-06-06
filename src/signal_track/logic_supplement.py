from __future__ import annotations

import json
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


class OpenAILogicSupplementer(LogicSupplementer):
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        web_research: bool = False,
        web_search_context_size: str = "medium",
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install LLM extras first: pip install -e .[llm]") from exc
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.web_research = web_research
        self.web_search_context_size = web_search_context_size

    def supplement(
        self,
        *,
        name: str,
        direction: Direction,
        source_logic: str,
        instruments: list[Instrument],
    ) -> LogicSupplement:
        instrument_lines = "\n".join(
            f"- {instrument.name} / {instrument.symbol} / {instrument.market.value} / {instrument.asset_type.value}"
            for instrument in instruments
        ) or f"- {name}"
        request = {
            "model": self.model,
            "instructions": (
                "你是资深亚洲对冲基金投研分析师。用户提供的投资信号逻辑不足时，"
                "请基于 3C-5M-3D-3T 框架生成可执行的后续跟踪逻辑。"
                "如果启用了 web search，请先围绕财务估值、行业竞争、最新动态至少做多维度检索，"
                "但仍不要编造未经验证的财务或行业数据；无法交叉验证的数据必须在 verification_notes 标明待验证。"
                "输出要服务于每日跟踪和是否平仓判断，而不是写完整投研报告。"
                "tracking_metrics 必须尽量包含可观察指标，例如价格行为、财报项目、行业价格、订单、利润率、估值或情绪。"
                "exit_conditions 必须写成可以被人工或系统复核的触发条件。"
            ),
            "input": [
                {
                    "role": "user",
                    "content": (
                        f"项目：{name}\n"
                        f"方向：{direction.value}\n"
                        f"标的：\n{instrument_lines}\n\n"
                        f"原始逻辑：\n{source_logic}"
                    ),
                }
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "tracking_logic_supplement",
                    "strict": True,
                    "schema": SUPPLEMENT_SCHEMA,
                }
            },
        }
        if self.web_research:
            request["tools"] = [web_search_tool(self.web_search_context_size)]
            request["tool_choice"] = "required"
        response = self.client.responses.create(**request)
        return logic_supplement_from_dict(json.loads(response.output_text))


def logic_supplement_from_dict(data: dict) -> LogicSupplement:
    return LogicSupplement(
        thesis=str(data.get("thesis") or ""),
        tracking_metrics=[str(item) for item in data.get("tracking_metrics", [])],
        exit_conditions=[str(item) for item in data.get("exit_conditions", [])],
        verification_notes=[str(item) for item in data.get("verification_notes", [])],
        confidence=float(data.get("confidence") or 0.5),
    )


def web_search_tool(search_context_size: str) -> dict[str, str]:
    return {"type": "web_search", "search_context_size": search_context_size}


def build_logic_supplementer(
    api_key: str | None,
    model: str,
    *,
    web_research: bool = False,
    web_search_context_size: str = "medium",
) -> LogicSupplementer | None:
    if not api_key:
        return None
    try:
        return OpenAILogicSupplementer(
            api_key,
            model,
            web_research=web_research,
            web_search_context_size=web_search_context_size,
        )
    except RuntimeError:
        return None
