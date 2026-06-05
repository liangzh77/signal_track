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
                    "direction",
                    "source_logic",
                    "observation_logic",
                    "logic_score",
                    "is_portfolio",
                    "weights",
                ],
                "properties": {
                    "instruments": {"type": "array", "items": {"type": "string"}},
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


class OpenAISignalExtractor:
    def __init__(self, api_key: str, model: str):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install LLM extras first: pip install -e .[llm]") from exc
        self.client = OpenAI(api_key=api_key)
        self.model = model

    def extract(self, content: str, source_hint: str | None = None) -> ExtractedInput:
        prompt = (
            "你是投资信号抽取器。请从用户提供的中文/英文投资信息中抽取可跟踪信号。"
            "如果逻辑不足，也要创建信号，并把 logic_score 设低。"
            "如果多个标的是一个明确组合，is_portfolio=true；否则拆成多个信号。"
            "weights 只在原文给出权重时填写，不能编造。"
            "source_logic 必须忠实于原文，observation_logic 必须忠实于原文，不要在这里补充研究逻辑。"
        )
        response = self.client.responses.create(
            model=self.model,
            instructions=prompt,
            input=[
                {
                    "role": "user",
                    "content": (
                        f"信息源提示：{source_hint or '未提供'}\n\n"
                        f"原文：\n{content}"
                    ),
                }
            ],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "investment_signal_extraction",
                    "strict": True,
                    "schema": SIGNAL_EXTRACTION_SCHEMA,
                }
            },
        )
        data = json.loads(response.output_text)
        return extracted_input_from_dict(data)


def extracted_input_from_dict(data: dict) -> ExtractedInput:
    return ExtractedInput(
        source_name=data.get("source_name"),
        needs_review=bool(data.get("needs_review", False)),
        notes=str(data.get("notes") or ""),
        signals=[
            ExtractedSignal(
                instruments=[str(item) for item in signal.get("instruments", [])],
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

