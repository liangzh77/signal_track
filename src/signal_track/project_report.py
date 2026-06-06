from __future__ import annotations

import json
from typing import Any

from .analytics import project_performance
from .db import Repository
from .project_summary import performance_summary


FRAMEWORK_SECTIONS = [
    ("3C", ["Cycle", "Change", "Certainty"]),
    ("5M", ["Market Space", "Market Share", "OPM", "Business Model", "Management"]),
    ("3D", ["ROE/PB", "External Change", "Sentiment/Valuation"]),
    ("3T", ["0-3 months", "3-15 months", "15+ months"]),
]


def build_project_report(repo: Repository, project_id: int) -> dict[str, Any] | None:
    row = repo.get_project_row(project_id)
    if not row:
        return None
    performance = project_performance(repo, project_id)
    logic_blocks = [dict(item) for item in repo.list_logic_blocks(project_id)]
    research_items = [normalize_research_item(dict(item)) for item in repo.list_research_items(project_id=project_id)]
    daily_checks = [dict(item) for item in repo.list_daily_checks(project_id=project_id)]
    legs = [dict(item) for item in repo.list_project_legs(project_id)]
    return {
        "title": f"{row['title']} 投研报告 | 基于风和3C-5M-3D-3T框架",
        "project": {
            "id": int(row["id"]),
            "title": row["title"],
            "source_name": row["source_name"],
            "status": row["status"],
            "direction": row["direction"],
            "entry_date": row["entry_date"],
            "closed_date": row["closed_date"],
            "logic_score": float(row["logic_score"]),
            "needs_review": bool(row["needs_review"]),
            "weight_needs_review": bool(row["weight_needs_review"]),
        },
        "instruments": [instrument_summary(leg) for leg in legs],
        "logic_blocks": logic_blocks,
        "research_items": research_items,
        "daily_checks": daily_checks,
        "performance": performance_summary(performance),
        "framework": framework_snapshot(logic_blocks, research_items),
        "data_verification": data_verification_summary(research_items),
    }


def render_project_report_markdown(report: dict[str, Any]) -> str:
    project = report["project"]
    lines = [
        f"# {report['title']}",
        "",
        "## 一、开篇：一句话定性",
        (
            f"{project['title']} 当前方向为 **{project['direction']}**，状态为 **{project['status']}**。"
            f"系统记录的逻辑完整度为 **{project['logic_score']:.2f}**，"
            f"{'仍需复核。' if project['needs_review'] else '暂不需要人工补充逻辑。'}"
        ),
        "",
        "## 二、项目概况",
        f"- 信息源：{project['source_name']}",
        f"- 开仓日期：{project['entry_date'] or '未记录'}",
        f"- 平仓日期：{project['closed_date'] or '未平仓'}",
        f"- 权重待确认：{'是' if project['weight_needs_review'] else '否'}",
        "",
        "### 标的",
    ]
    lines.extend(instrument_lines(report["instruments"]))
    lines.extend(
        [
            "",
            "## 三、原始与补充逻辑",
            *logic_lines(report["logic_blocks"]),
            "",
            "## 四、3C-5M-3D-3T 跟踪框架",
        ]
    )
    for section in report["framework"]:
        lines.append(f"### {section['name']}")
        if section["items"]:
            lines.extend(f"- {item}" for item in section["items"])
        else:
            lines.append("- 暂无已结构化内容；按研究验证项继续补充。")
        lines.append("")
    lines.extend(
        [
            "## 五、当前价格与组合曲线摘要",
            performance_lines(report["performance"]),
            "",
            "## 六、最新检查记录",
            *daily_check_lines(report["daily_checks"]),
            "",
            "## 七、数据验证记录",
            f"- 已验证：{report['data_verification']['verified_count']}",
            f"- 待验证/未验证：{report['data_verification']['pending_count']}",
            f"- 已矛盾：{report['data_verification']['contradicted_count']}",
            *research_item_lines(report["research_items"]),
            "",
            "## 八、免责声明",
            "本报告基于系统已录入的信息、价格数据和研究验证项自动生成；未标注为已验证的数据不得视为事实结论。本报告不构成投资建议，投资有风险，决策需独立判断。",
            "",
        ]
    )
    return "\n".join(lines)


def instrument_summary(leg: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": leg["symbol"],
        "provider_symbol": leg["provider_symbol"],
        "name": leg["name"],
        "market": leg["market"],
        "asset_type": leg["asset_type"],
        "exchange": leg["exchange"],
        "currency": leg["currency"],
        "direction": leg["direction"],
        "weight": float(leg["weight"]),
        "entry_price": leg["entry_price"],
        "entry_date": leg["entry_date"],
    }


def normalize_research_item(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata")
    if isinstance(metadata, str):
        try:
            item["metadata"] = json.loads(metadata or "{}")
        except json.JSONDecodeError:
            item["metadata"] = {"raw": metadata}
    return item


def framework_snapshot(logic_blocks: list[dict[str, Any]], research_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    search_space = [
        *(str(block.get("content") or "") for block in logic_blocks),
        *(str(item.get("content") or "") for item in research_items),
    ]
    sections = []
    for name, keywords in FRAMEWORK_SECTIONS:
        items = []
        for text in search_space:
            if any(keyword.lower() in text.lower() or keyword in text for keyword in [name, *keywords]):
                items.append(first_sentence(text))
            if len(items) >= 5:
                break
        sections.append({"name": name, "items": dedupe(items)})
    return sections


def data_verification_summary(research_items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "verified_count": sum(1 for item in research_items if item.get("status") == "verified"),
        "pending_count": sum(1 for item in research_items if item.get("status") in {"pending", "unverified"}),
        "contradicted_count": sum(1 for item in research_items if item.get("status") == "contradicted"),
    }


def instrument_lines(instruments: list[dict[str, Any]]) -> list[str]:
    if not instruments:
        return ["- 暂无标的。"]
    return [
        (
            f"- **{item['symbol']}** / {item['name']} / {item['market']} / {item['asset_type']}："
            f"{item['direction']}，权重 {item['weight']:.2f}，"
            f"入场价 {item['entry_price'] if item['entry_price'] is not None else '未记录'}"
        )
        for item in instruments
    ]


def logic_lines(logic_blocks: list[dict[str, Any]]) -> list[str]:
    if not logic_blocks:
        return ["暂无逻辑块。"]
    lines = []
    for block in logic_blocks:
        lines.append(f"### {block['logic_type']} / 置信度 {float(block['confidence']):.2f}")
        lines.append(str(block["content"]).strip())
        lines.append("")
    return lines


def performance_lines(performance: dict[str, Any]) -> str:
    latest_date = performance.get("latest_date") or "暂无价格"
    return_pct = performance.get("return_pct")
    return_text = f"{return_pct:.2%}" if isinstance(return_pct, (int, float)) else "暂无"
    missing = ", ".join(performance.get("missing_price_symbols") or []) or "无"
    leg_lines = [
        f"- {leg['symbol']}：收益 {format_pct(leg.get('return_pct'))}，最新价 {leg.get('latest_price') or '暂无'}"
        for leg in performance.get("legs", [])
    ]
    return "\n".join(
        [
            f"- 最新价格日期：{latest_date}",
            f"- 项目当前收益：**{return_text}**",
            f"- 曲线点数：{performance.get('point_count', 0)}",
            f"- 缺失价格标的：{missing}",
            *leg_lines,
        ]
    )


def daily_check_lines(daily_checks: list[dict[str, Any]]) -> list[str]:
    if not daily_checks:
        return ["- 暂无检查记录。"]
    return [
        f"- {item['check_date']}：**{item['conclusion']}**，{item['summary']}"
        for item in daily_checks[:10]
    ]


def research_item_lines(research_items: list[dict[str, Any]]) -> list[str]:
    if not research_items:
        return ["- 暂无研究验证项。"]
    lines = ["", "### 研究验证项"]
    for item in research_items:
        source_note = item.get("source_note") or "未标注来源"
        lines.append(f"- [{item['status']}] {item['item_type']}：{item['content']}（{source_note}）")
    return lines


def first_sentence(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= 220:
        return compact
    return compact[:217] + "..."


def dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def format_pct(value: Any) -> str:
    return f"{value:.2%}" if isinstance(value, (int, float)) else "暂无"
