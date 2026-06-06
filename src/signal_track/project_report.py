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

DIMENSION_KEYWORDS = {
    "cycle": ["3C", "cycle", "周期", "景气", "库存", "上行", "下行"],
    "change": ["3C", "change", "变化", "拐点", "催化", "份额", "竞争格局"],
    "certainty": ["3C", "certainty", "确定性", "验证", "交叉验证", "风险"],
    "tam": ["5M", "tam", "market space", "市场空间", "行业规模", "渗透率", "天花板"],
    "share": ["5M", "market share", "市场份额", "竞争", "护城河", "份额"],
    "opm": ["5M", "opm", "利润率", "毛利率", "经营利润率", "盈利"],
    "model": ["5M", "business model", "商业模式", "现金流", "复购", "收款"],
    "management": ["5M", "management", "管理", "治理", "执行", "激励"],
    "roe": ["3D", "roe", "pb", "内生价值", "估值", "资产"],
    "external": ["3D", "external", "外延", "政策", "技术", "周期性变化"],
    "sentiment": ["3D", "sentiment", "情绪", "估值", "pe", "pb", "分位"],
    "short_term": ["3T", "0-3", "短期", "催化", "风险事件"],
    "medium_term": ["3T", "3-15", "中期", "季度", "兑现"],
    "long_term": ["3T", "15+", "长期", "复利", "战略", "趋势"],
}

SCORECARD_ROWS = [
    ("周期位置", "cycle"),
    ("变化方向", "change"),
    ("确定性", "certainty"),
    ("市场空间", "tam"),
    ("竞争地位", "share"),
    ("盈利质量", "opm"),
    ("商业模式", "model"),
    ("管理团队", "management"),
    ("估值吸引力", "sentiment"),
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
    performance_snapshot = performance_summary(performance)
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
        "performance": performance_snapshot,
        "framework": framework_snapshot(logic_blocks, research_items),
        "data_verification": data_verification_summary(research_items),
        "scorecard": scorecard_snapshot(row, performance_snapshot, research_items),
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
            f"{'仍需复核和数据验证。' if project['needs_review'] else '已有可执行跟踪逻辑，但仍以公开数据验证为准。'}"
        ),
        "",
        "基础信息：",
        f"- 信息源：{project['source_name']}",
        f"- 开仓日期：{project['entry_date'] or '未记录'}",
        f"- 平仓日期：{project['closed_date'] or '未平仓'}",
        f"- 权重待确认：{'是' if project['weight_needs_review'] else '否'}",
        f"- 价格窗口：{report['performance'].get('window_start') or '未形成'} 至 {report['performance'].get('window_end') or '未形成'}",
        "",
        "标的：",
    ]
    lines.extend(instrument_lines(report["instruments"]))
    lines.extend(
        [
            "",
            "## 二、3C分析：投资哲学定位",
            *dimension_lines("C1 周期（Cycle）", "行业与公司所处周期", "cycle", report),
            *dimension_lines("C2 变化（Change）", "正在发生的关键变化", "change", report),
            *dimension_lines("C3 确定性（Certainty）", "变化兑现概率与不确定性", "certainty", report),
            framework_summary_line("3C小结", ["cycle", "change", "certainty"], report),
            "",
            "## 三、5M分析：企业价值拆解",
            *dimension_lines("M1 市场空间（TAM）", "行业空间、增速与天花板", "tam", report),
            *dimension_lines("M2 市场份额（Market Share）", "份额趋势、竞争格局与护城河", "share", report),
            *dimension_lines("M3 经营利润率（OPM）", "利润率水平、趋势和驱动因素", "opm", report),
            *dimension_lines("M4 商业模式（Business Model）", "赚钱方式、现金流和脆弱环节", "model", report),
            *dimension_lines("M5 管理团队（Management）", "战略能力、治理和激励", "management", report),
            "",
            "## 四、3D分析：股价驱动力",
            *dimension_lines("D1 内生价值（ROE驱动）", "ROE、PB与创造现金能力", "roe", report),
            *dimension_lines("D2 外延变化", "结构性或周期性重估因素", "external", report),
            *dimension_lines("D3 情绪与估值", "市场情绪、估值分位与赔率", "sentiment", report),
            framework_summary_line("3D小结", ["roe", "external", "sentiment"], report),
            "",
            "## 五、3T分析：时间框架",
            *dimension_lines("T1 短期（0-3个月）", "催化剂、风险事件和资金面", "short_term", report),
            *dimension_lines("T2 中期（3-15个月）", "季度兑现路径和中期确定性", "medium_term", report),
            *dimension_lines("T3 长期（15个月以上）", "长期战略价值和核心风险", "long_term", report),
            "",
            "## 六、风和视角：特色分析",
            *fenghe_lines(report),
            "",
            "## 七、综合评估",
            "**核心结论**：",
            comprehensive_conclusion(report),
            "",
            "**评分卡**：",
            "",
            "| 维度 | 评分 | 一句话理由 |",
            "|------|------|------------|",
            *scorecard_lines(report["scorecard"]),
            "",
            "**主要风险**：",
            *risk_lines(report),
            "",
            "**关键跟踪指标**：",
            *key_tracking_lines(report),
            "",
            "## 八、数据来源与免责声明",
            "**主要数据来源**：",
            *source_lines(report),
            "",
            "**数据验证记录**：",
            f"- 已验证：{report['data_verification']['verified_count']}",
            f"- 待验证/未验证：{report['data_verification']['pending_count']}",
            f"- 已矛盾：{report['data_verification']['contradicted_count']}",
            *research_item_lines(report["research_items"]),
            "",
            "**原始与补充逻辑记录**：",
            *logic_lines(report["logic_blocks"]),
            "",
            "**免责声明**：本报告基于系统已录入的信息、价格数据和研究验证项自动生成；未标注为已验证的数据不得视为事实结论。本报告不构成投资建议，投资有风险，决策需独立判断。",
            "",
        ]
    )
    return "\n".join(lines)


def dimension_lines(heading: str, focus: str, key: str, report: dict[str, Any]) -> list[str]:
    evidence = matching_evidence(report, DIMENSION_KEYWORDS[key], limit=4)
    if evidence:
        conclusion = "已有可跟踪线索；结论仍以验证项和后续日检为准。"
        bullets = evidence
    else:
        conclusion = "公开数据尚未在系统内完成交叉验证，当前不做事实断言。"
        bullets = [f"待验证：{focus}；需要至少两个独立来源或后续检查记录确认。"]
    return [
        f"### {heading}",
        f"- 结论：{conclusion}",
        *[f"- 依据：{item}" for item in bullets],
        "",
    ]


def framework_summary_line(title: str, keys: list[str], report: dict[str, Any]) -> str:
    evidence_count = sum(len(matching_evidence(report, DIMENSION_KEYWORDS[key], limit=1)) for key in keys)
    if evidence_count:
        return f"**{title}**：已有 {evidence_count} 类线索进入跟踪，但仍需要通过研究验证项和每日检查持续校验。"
    return f"**{title}**：框架已建立，当前主要缺口是公开数据交叉验证。"


def fenghe_lines(report: dict[str, Any]) -> list[str]:
    project = report["project"]
    lines = []
    if project["needs_review"] or report["data_verification"]["pending_count"]:
        lines.extend(
            [
                "### 缺陷投资",
                "- 结论：当前最大的缺陷不是观点本身，而是部分关键数据仍待验证；缺陷可量化、可跟踪，应通过研究验证项逐步关闭。",
                "",
            ]
        )
    if project["direction"] == "short":
        lines.extend(
            [
                "### 宴席理论 / 踩烂理论（做空视角）",
                "- 结论：若后续数据继续显示盈利、估值或商业模式恶化，应优先检查 party 是否进入散场阶段；目前未验证数据不得直接作为做空事实。",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "### 逆势投资",
                "- 结论：若市场情绪短期恶化但核心 3C 与 5M 数据未被证伪，可以作为逆势复核场景；若关键验证项被证伪，应优先降低权重或退出。",
                "",
            ]
        )
    lines.extend(
        [
            "### 权重建议",
            "- 结论：系统不自动给出投资建议权重；组合权重以用户输入为准。若缺少权重，系统按等权跟踪并标记待确认。",
        ]
    )
    return lines


def comprehensive_conclusion(report: dict[str, Any]) -> str:
    project = report["project"]
    performance = report["performance"]
    latest_return = performance.get("return_pct")
    return_text = format_pct(latest_return)
    latest_check = report["daily_checks"][0] if report["daily_checks"] else None
    check_text = f"最新检查结论为 **{latest_check['conclusion']}**。" if latest_check else "尚无每日检查结论。"
    review_text = "仍有待验证项，不能把未核实信息当作事实。" if report["data_verification"]["pending_count"] else "当前未发现待验证项。"
    return (
        f"{project['title']} 当前按 **{project['direction']}** 跟踪，项目收益为 **{return_text}**。"
        f"{check_text}{review_text}"
    )


def risk_lines(report: dict[str, Any]) -> list[str]:
    risks = []
    if report["data_verification"]["pending_count"]:
        risks.append("- 数据验证风险：财务、行业或管理层信息仍有待交叉验证。")
    if report["performance"].get("missing_price_symbols"):
        missing = ", ".join(report["performance"]["missing_price_symbols"])
        risks.append(f"- 行情完整性风险：{missing} 缺少可用价格，曲线和收益需复核。")
    if report["project"]["weight_needs_review"]:
        risks.append("- 组合权重风险：当前采用等权或待确认权重，组合收益曲线可能偏离真实配置。")
    risks.append("- 逻辑证伪风险：若原始开仓逻辑或系统补充逻辑被后续数据否定，应触发退出复核。")
    return risks[:4]


def key_tracking_lines(report: dict[str, Any]) -> list[str]:
    items = matching_evidence(report, ["tracking_metric", "关键跟踪", "track", "指标"], limit=3)
    if not items:
        items = [
            "价格相对入场价、移动均线和止损/止盈阈值的变化。",
            "财务与估值数据是否支持原始开仓逻辑。",
            "行业竞争、份额和管理层动态是否出现证伪信号。",
        ]
    return [f"- {item}" for item in items[:3]]


def source_lines(report: dict[str, Any]) -> list[str]:
    lines = [
        f"- 原始信息源：{report['project']['source_name']}",
        "- 系统逻辑块：source_logic、source_update、system_logic 和 close_logic。",
        "- 本地价格数据：price_bars 表中的日线数据及其 provider/provider_symbol 字段。",
    ]
    if report["daily_checks"]:
        lines.append("- 每日检查记录：daily_checks 表中的结论、触发规则和证据。")
    if report["research_items"]:
        lines.append("- 研究验证项：research_items 表中的人工或系统标注来源。")
    return lines


def scorecard_lines(scorecard: list[dict[str, Any]]) -> list[str]:
    return [f"| {item['dimension']} | {item['score']}/10 | {item['reason']} |" for item in scorecard]


def scorecard_snapshot(row: Any, performance: dict[str, Any], research_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verified_count = sum(1 for item in research_items if item.get("status") == "verified")
    pending_count = sum(1 for item in research_items if item.get("status") in {"pending", "unverified"})
    logic_score = float(row["logic_score"])
    normalized_logic_score = logic_score * 10 if 0 <= logic_score <= 1 else logic_score
    base = clamp_score(round(normalized_logic_score))
    scorecard = []
    for dimension, key in SCORECARD_ROWS:
        evidence = matching_research_items(research_items, DIMENSION_KEYWORDS[key], limit=1)
        score = base
        reason = "按当前逻辑完整度给出保守评分。"
        if evidence:
            score = clamp_score(score + 1)
            reason = "已有对应研究或跟踪线索，但仍需持续验证。"
        if pending_count and key in {"certainty", "tam", "share", "opm", "management", "sentiment"}:
            score = clamp_score(score - 1)
            reason = "存在待验证数据，评分保持保守。"
        if verified_count and key in {"certainty", "opm", "sentiment"}:
            score = clamp_score(score + 1)
            reason = "已有验证项支持，评分小幅上调。"
        if key == "change":
            performance_bias = performance_change_bias(performance.get("return_pct"))
            score = clamp_score(score + performance_bias)
            if performance_bias:
                reason = "价格表现与当前方向阶段性一致，但不能替代基本面验证。"
        scorecard.append({"dimension": dimension, "score": score, "reason": reason})
    return scorecard


def performance_change_bias(return_pct: Any) -> int:
    if not isinstance(return_pct, (int, float)):
        return 0
    if return_pct > 0.03:
        return 1
    if return_pct < -0.03:
        return -1
    return 0


def clamp_score(value: int) -> int:
    return max(1, min(10, value))


def matching_evidence(report: dict[str, Any], keywords: list[str], limit: int = 4) -> list[str]:
    items = [
        f"{item['item_type']}：{item['content']}"
        for item in matching_research_items(report["research_items"], keywords, limit=limit)
    ]
    if len(items) < limit:
        for block in report["logic_blocks"]:
            text = str(block.get("content") or "")
            if text_matches(text, keywords):
                items.append(f"{block.get('logic_type')}：{first_sentence(text)}")
            if len(items) >= limit:
                break
    return dedupe(items)[:limit]


def matching_research_items(research_items: list[dict[str, Any]], keywords: list[str], limit: int = 4) -> list[dict[str, Any]]:
    matches = []
    for item in research_items:
        metadata = item.get("metadata") or {}
        haystack = " ".join(
            [
                str(item.get("item_type") or ""),
                str(item.get("content") or ""),
                str(item.get("source_note") or ""),
                json.dumps(metadata, ensure_ascii=False) if isinstance(metadata, dict) else str(metadata),
            ]
        )
        if text_matches(haystack, keywords):
            matches.append(item)
        if len(matches) >= limit:
            break
    return matches


def text_matches(text: str, keywords: list[str]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


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
