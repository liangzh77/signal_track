from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .analytics import ProjectPerformance
from .db import Repository


@dataclass(frozen=True)
class RuleHit:
    rule_type: str
    message: str
    severity: str = "exit_signal"


def evaluate_project_rules(
    repo: Repository,
    project_id: int,
    performance: ProjectPerformance,
    check_date: date,
) -> list[RuleHit]:
    logic_text = "\n".join(block["content"] for block in repo.list_logic_blocks(project_id))
    hits: list[RuleHit] = []
    hits.extend(evaluate_return_rules(logic_text, performance))
    hits.extend(evaluate_moving_average_rules(repo, project_id, logic_text, check_date))
    return hits


def evaluate_return_rules(logic_text: str, performance: ProjectPerformance) -> list[RuleHit]:
    if performance.return_pct is None:
        return []
    hits: list[RuleHit] = []

    loss_thresholds = extract_percent_thresholds(
        logic_text,
        (
            "回撤",
            "亏损",
            "跌幅",
            "跌破",
            "止损",
            "drawdown",
            "loss",
            "stop loss",
            "stop-loss",
            "downside",
        ),
    )
    for threshold in loss_thresholds:
        if performance.return_pct <= -threshold:
            hits.append(
                RuleHit(
                    "return_drawdown",
                    f"项目收益 {performance.return_pct:.2%}，触发回撤/亏损阈值 {threshold:.2%}",
                )
            )

    profit_thresholds = extract_percent_thresholds(
        logic_text,
        (
            "止盈",
            "涨幅",
            "收益",
            "盈利",
            "take profit",
            "take-profit",
            "profit",
            "gain",
            "upside",
            "return",
        ),
    )
    for threshold in profit_thresholds:
        if performance.return_pct >= threshold:
            hits.append(
                RuleHit(
                    "return_take_profit",
                    f"项目收益 {performance.return_pct:.2%}，触发止盈/收益阈值 {threshold:.2%}",
                )
            )
    return hits


def evaluate_moving_average_rules(
    repo: Repository,
    project_id: int,
    logic_text: str,
    check_date: date,
) -> list[RuleHit]:
    windows = extract_moving_average_windows(logic_text)
    if not windows:
        return []

    hits: list[RuleHit] = []
    for leg in repo.list_project_legs(project_id):
        for window in windows:
            bars = repo.list_price_bars(int(leg["instrument_id"]), end_date=check_date.isoformat())
            closes = [float(bar["close"]) for bar in bars if bar["close"] is not None]
            if len(closes) < window:
                continue
            latest = closes[-1]
            average = sum(closes[-window:]) / window
            if latest < average:
                hits.append(
                    RuleHit(
                        "moving_average_break",
                        f"{leg['symbol']} 收盘价 {latest:.2f} 跌破 {window} 日均线 {average:.2f}",
                    )
                )
    return hits


def extract_percent_thresholds(logic_text: str, keywords: tuple[str, ...]) -> list[float]:
    thresholds: list[float] = []
    for keyword in keywords:
        pattern = rf"{keyword}[^\d%％]{{0,24}}(\d+(?:\.\d+)?)\s*[%％]"
        for value in re.findall(pattern, logic_text, flags=re.IGNORECASE):
            thresholds.append(float(value) / 100)
    return sorted(set(thresholds))


def extract_moving_average_windows(logic_text: str) -> list[int]:
    patterns = [
        r"跌破\s*(\d{1,3})\s*日(?:线|均线)",
        r"(?:breaks?|falls?|drops?)\s+below[^\n.;]{0,40}?(\d{1,3})\s*(?:day|d)[-\s]*(?:moving\s+average|ma)",
        r"(?:below|under)[^\n.;]{0,40}?(\d{1,3})\s*(?:day|d)[-\s]*(?:moving\s+average|ma)",
        r"(?:breaks?|falls?|drops?)\s+below[^\n.;]{0,20}?MA\s*(\d{1,3})",
        r"\bMA\s*(\d{1,3})[^\n.;]{0,30}?(?:break|below|under)",
    ]
    windows: set[int] = set()
    for pattern in patterns:
        for value in re.findall(pattern, logic_text, flags=re.IGNORECASE):
            window = int(value)
            if 1 <= window <= 250:
                windows.add(window)
    return sorted(windows)
