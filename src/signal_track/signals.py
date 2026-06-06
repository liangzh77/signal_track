from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from .db import Repository
from .extraction import ExtractedInput, ExtractedSignal
from .logic_supplement import LogicSupplement, LogicSupplementer
from .models import Direction, ProjectStatus
from .resolver import InstrumentResolver


LONG_WORDS = ("做多", "买入", "开多", "long", "看多", "多头")
SHORT_WORDS = ("做空", "卖空", "开空", "short", "看空", "空头")
EXIT_WORDS = ("平仓", "止盈", "止损", "退出", "close", "exit")
LOGIC_WORDS = ("因为", "逻辑", "观察", "跟踪", "如果", "若", "触发", "催化", "风险", "验证")
PORTFOLIO_WORDS = ("组合", "一篮子", "篮子", "配对", "权重", "占比", "portfolio", "basket", "pair trade")


@dataclass(frozen=True)
class IngestResult:
    raw_input_id: int
    project_ids: list[int]
    resolved_symbols: list[str]
    logic_score: float
    system_logic_added: bool


class SignalIngestor:
    def __init__(
        self,
        repo: Repository,
        resolver: InstrumentResolver,
        logic_supplementer: LogicSupplementer | None = None,
    ):
        self.repo = repo
        self.resolver = resolver
        self.logic_supplementer = logic_supplementer

    def ingest(
        self,
        source_name: str,
        content: str,
        as_portfolio: bool = False,
        weights: dict[str, float] | None = None,
        extraction: ExtractedInput | None = None,
        attachment_path: str | None = None,
    ) -> IngestResult:
        source_id = self.repo.get_or_create_source(source_name or "manual")
        raw_input_id = self.repo.add_raw_input(source_id, content, attachment_path=attachment_path)
        if extraction and extraction.signals:
            return self._ingest_extraction(source_id, raw_input_id, content, extraction)

        resolutions = self._find_instruments(content)
        direction = detect_direction(content)
        logic_score = score_tracking_logic(content)
        needs_review = logic_score < 6
        system_logic_added = needs_review

        if is_close_action(content) and resolutions:
            closed_project_ids = self._close_existing_projects(raw_input_id, content, resolutions, logic_score)
            if closed_project_ids:
                return IngestResult(
                    raw_input_id=raw_input_id,
                    project_ids=closed_project_ids,
                    resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
                    logic_score=logic_score,
                    system_logic_added=False,
                )

        weights = weights or parse_weight_hints(content, resolutions)
        as_portfolio = as_portfolio or should_treat_as_portfolio(content, resolutions, weights)
        updated_project_ids = (
            self._append_existing_portfolio_updates(source_id, content, resolutions, direction, logic_score)
            if as_portfolio
            else self._append_existing_project_updates(source_id, content, resolutions, direction, logic_score)
        )
        if updated_project_ids:
            return IngestResult(
                raw_input_id=raw_input_id,
                project_ids=updated_project_ids,
                resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
                logic_score=logic_score,
                system_logic_added=False,
            )

        if not resolutions:
            project_id = self.repo.create_tracking_project(
                title="未识别标的跟踪项目",
                source_id=source_id,
                raw_input_id=raw_input_id,
                status=ProjectStatus.NEEDS_REVIEW.value,
                direction=direction.value,
                entry_date=date.today().isoformat(),
                logic_score=logic_score,
                needs_review=True,
                metadata={"raw_extract_status": "no_instrument_resolved"},
            )
            self.repo.add_logic_block(project_id, "source_logic", content, logic_score / 10, [content[:240]])
            self._add_system_logic_block(project_id, "未识别标的", direction, content, [], 0.35)
            return IngestResult(raw_input_id, [project_id], [], logic_score, True)

        project_ids: list[int] = []
        if as_portfolio:
            project_id = self._create_portfolio_project(
                source_id, raw_input_id, content, resolutions, direction, logic_score, needs_review, weights
            )
            project_ids.append(project_id)
        else:
            for resolution in resolutions:
                project_id = self._create_single_project(
                    source_id, raw_input_id, content, resolution, direction, logic_score, needs_review
                )
                project_ids.append(project_id)

        return IngestResult(
            raw_input_id=raw_input_id,
            project_ids=project_ids,
            resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
            logic_score=logic_score,
            system_logic_added=system_logic_added,
        )

    def _ingest_extraction(
        self,
        source_id: int,
        raw_input_id: int,
        original_content: str,
        extraction: ExtractedInput,
    ) -> IngestResult:
        project_ids: list[int] = []
        resolved_symbols: list[str] = []
        system_logic_added = False

        for signal in extraction.signals:
            resolutions = self._resolve_extracted_signal(signal)
            direction = Direction(signal.direction)
            logic_score = max(0.0, min(10.0, signal.logic_score))
            needs_review = logic_score < 6 or extraction.needs_review
            system_logic_added = system_logic_added or needs_review
            source_logic = signal.source_logic or summarize_source_logic(original_content)
            if signal.observation_logic:
                source_logic = f"{source_logic}\n\n观察逻辑：{signal.observation_logic}"

            if is_extracted_close_action(signal, source_logic, original_content) and resolutions:
                closed_project_ids = self._close_existing_projects(raw_input_id, source_logic, resolutions, logic_score)
                if closed_project_ids:
                    project_ids.extend(closed_project_ids)
                    resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                    continue

            if signal.is_portfolio:
                updated_project_ids = self._append_existing_portfolio_updates(
                    source_id,
                    source_logic,
                    resolutions,
                    direction,
                    logic_score,
                )
                if updated_project_ids:
                    project_ids.extend(updated_project_ids)
                    resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                    continue
            else:
                updated_project_ids = self._append_existing_project_updates(
                    source_id,
                    source_logic,
                    resolutions,
                    direction,
                    logic_score,
                )
                if updated_project_ids:
                    project_ids.extend(updated_project_ids)
                    resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                    continue

            if not resolutions:
                project_id = self.repo.create_tracking_project(
                    title="未识别标的跟踪项目",
                    source_id=source_id,
                    raw_input_id=raw_input_id,
                    status=ProjectStatus.NEEDS_REVIEW.value,
                    direction=direction.value,
                    entry_date=date.today().isoformat(),
                    logic_score=logic_score,
                    needs_review=True,
                    metadata={"raw_extract_status": "no_instrument_resolved", "extractor": "structured"},
                )
                self.repo.add_logic_block(project_id, "source_logic", source_logic, logic_score / 10, [original_content[:240]])
                self._add_system_logic_block(project_id, "未识别标的", direction, source_logic, [], 0.35)
                project_ids.append(project_id)
                system_logic_added = True
                continue

            if signal.is_portfolio:
                project_id = self._create_portfolio_project(
                    source_id,
                    raw_input_id,
                    source_logic,
                    resolutions,
                    direction,
                    logic_score,
                    needs_review,
                    signal.weights,
                )
                project_ids.append(project_id)
            else:
                for resolution in resolutions:
                    project_id = self._create_single_project(
                        source_id,
                        raw_input_id,
                        source_logic,
                        resolution,
                        direction,
                        logic_score,
                        needs_review,
                    )
                    project_ids.append(project_id)
            resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)

        average_score = (
            sum(signal.logic_score for signal in extraction.signals) / len(extraction.signals)
            if extraction.signals
            else 0.0
        )
        return IngestResult(
            raw_input_id=raw_input_id,
            project_ids=project_ids,
            resolved_symbols=resolved_symbols,
            logic_score=round(average_score, 2),
            system_logic_added=system_logic_added,
        )

    def _resolve_extracted_signal(self, signal: ExtractedSignal):
        found = []
        seen = set()
        for instrument_name in signal.instruments:
            resolution = self.resolver.resolve(instrument_name)
            if resolution and resolution.instrument.symbol not in seen:
                found.append(resolution)
                seen.add(resolution.instrument.symbol)
        return found

    def _close_existing_projects(self, raw_input_id: int, content: str, resolutions, logic_score: float) -> list[int]:
        del raw_input_id
        closed_ids: list[int] = []
        closed_at = date.today().isoformat()
        for resolution in resolutions:
            for project_id in self.repo.find_active_project_ids_by_symbol(resolution.instrument.symbol):
                self.repo.close_project(
                    project_id,
                    closed_at,
                    metadata={
                        "close_reason": summarize_source_logic(content),
                        "closed_by_signal": True,
                    },
                )
                self.repo.add_logic_block(
                    project_id,
                    "close_logic",
                    summarize_source_logic(content),
                    logic_score / 10,
                    [content[:240]],
                )
                closed_ids.append(project_id)
        return sorted(set(closed_ids))

    def _append_existing_project_updates(
        self,
        source_id: int,
        content: str,
        resolutions,
        direction: Direction,
        logic_score: float,
    ) -> list[int]:
        updated_ids: list[int] = []
        direction_filter = None if direction == Direction.NEUTRAL else direction.value
        for resolution in resolutions:
            project_ids = self.repo.find_active_project_ids_by_source_symbol(
                source_id,
                resolution.instrument.symbol,
                direction=direction_filter,
            )
            for project_id in project_ids:
                self.repo.add_logic_block(
                    project_id,
                    "source_update",
                    summarize_source_logic(content),
                    logic_score / 10,
                    [content[:240]],
                )
                updated_ids.append(project_id)
        return sorted(set(updated_ids))

    def _append_existing_portfolio_updates(
        self,
        source_id: int,
        content: str,
        resolutions,
        direction: Direction,
        logic_score: float,
    ) -> list[int]:
        direction_filter = None if direction == Direction.NEUTRAL else direction.value
        symbols = [resolution.instrument.symbol for resolution in resolutions]
        updated_ids = self.repo.find_active_project_ids_by_source_symbols(
            source_id,
            symbols,
            direction=direction_filter,
        )
        for project_id in updated_ids:
            self.repo.add_logic_block(
                project_id,
                "source_update",
                summarize_source_logic(content),
                logic_score / 10,
                [content[:240]],
            )
        return updated_ids

    def _find_instruments(self, content: str):
        found = []
        seen = set()
        probes = extract_probe_terms(content)
        for probe in probes:
            resolution = self.resolver.resolve(probe)
            if resolution and resolution.instrument.symbol not in seen:
                found.append(resolution)
                seen.add(resolution.instrument.symbol)
        return found

    def _create_single_project(
        self,
        source_id: int,
        raw_input_id: int,
        content: str,
        resolution,
        direction: Direction,
        logic_score: float,
        needs_review: bool,
    ) -> int:
        instrument_id = self.repo.upsert_instrument(resolution.instrument)
        project_id = self.repo.create_tracking_project(
            title=f"{resolution.instrument.name} {direction_label(direction)}跟踪",
            source_id=source_id,
            raw_input_id=raw_input_id,
            status=(ProjectStatus.NEEDS_REVIEW if needs_review else ProjectStatus.ACTIVE).value,
            direction=direction.value,
            entry_date=date.today().isoformat(),
            logic_score=logic_score,
            needs_review=needs_review,
            metadata={"resolution_confidence": resolution.confidence, "resolution_reason": resolution.reason},
        )
        self.repo.add_project_leg(project_id, instrument_id, direction.value, 1.0)
        self.repo.add_logic_block(project_id, "source_logic", summarize_source_logic(content), logic_score / 10, [content[:240]])
        if needs_review:
            self._add_system_logic_block(
                project_id,
                resolution.instrument.name,
                direction,
                content,
                [resolution.instrument],
                0.62,
            )
        return project_id

    def _create_portfolio_project(
        self,
        source_id: int,
        raw_input_id: int,
        content: str,
        resolutions,
        direction: Direction,
        logic_score: float,
        needs_review: bool,
        weights: dict[str, float] | None,
    ) -> int:
        weights = weights or {}
        default_weight = round(1 / len(resolutions), 6)
        weight_needs_review = not weights
        title = "组合跟踪：" + " / ".join(resolution.instrument.name for resolution in resolutions)
        project_id = self.repo.create_tracking_project(
            title=title,
            source_id=source_id,
            raw_input_id=raw_input_id,
            status=(ProjectStatus.NEEDS_REVIEW if needs_review else ProjectStatus.ACTIVE).value,
            direction=direction.value,
            entry_date=date.today().isoformat(),
            logic_score=logic_score,
            needs_review=needs_review,
            weight_needs_review=weight_needs_review,
            metadata={"portfolio": True},
        )
        for resolution in resolutions:
            instrument_id = self.repo.upsert_instrument(resolution.instrument)
            weight = resolve_weight_for_instrument(weights, resolution.instrument, default_weight)
            self.repo.add_project_leg(project_id, instrument_id, direction.value, weight)
        self.repo.add_logic_block(project_id, "source_logic", summarize_source_logic(content), logic_score / 10, [content[:240]])
        if needs_review or weight_needs_review:
            self._add_system_logic_block(
                project_id,
                title,
                direction,
                content,
                [resolution.instrument for resolution in resolutions],
                0.62,
            )
        return project_id

    def _add_system_logic_block(
        self,
        project_id: int,
        name: str,
        direction: Direction,
        source_logic: str,
        instruments: list,
        fallback_confidence: float,
    ) -> None:
        content, confidence, evidence, research_items = self._system_logic_payload(
            name,
            direction,
            source_logic,
            instruments,
            fallback_confidence,
        )
        self.repo.add_logic_block(project_id, "system_logic", content, confidence, evidence)
        self.repo.add_research_items(project_id, research_items)

    def _system_logic_payload(
        self,
        name: str,
        direction: Direction,
        source_logic: str,
        instruments: list,
        fallback_confidence: float,
    ) -> tuple[str, float, list[str], list[dict[str, object]]]:
        if self.logic_supplementer:
            try:
                supplement = self.logic_supplementer.supplement(
                    name=name,
                    direction=direction,
                    source_logic=source_logic,
                    instruments=instruments,
                )
                if supplement.thesis.strip():
                    return (
                        supplement.to_block(),
                        supplement.confidence,
                        supplement_evidence(supplement),
                        supplement_research_items(supplement),
                    )
            except Exception:
                pass
        return build_system_logic(name, direction), fallback_confidence, fallback_evidence(name), fallback_research_items(name)


def supplement_evidence(supplement: LogicSupplement) -> list[str]:
    evidence: list[str] = []
    evidence.extend(f"tracking_metric: {item}" for item in supplement.tracking_metrics)
    evidence.extend(f"exit_condition: {item}" for item in supplement.exit_conditions)
    evidence.extend(f"verification_note: {item}" for item in supplement.verification_notes)
    evidence.append(f"supplement_confidence: {supplement.confidence:.2f}")
    return evidence


def fallback_evidence(name: str) -> list[str]:
    return [
        "source: local 3C-5M-3D-3T fallback",
        "research_playbook: Step 1 financial/valuation, Step 2 industry/competition, Step 3 latest dynamics/management",
        "cross_validation_rule: core financial data requires at least two independent sources before being marked verified",
        f"verification_note: {name} requires external financial, industry, and news verification before high-conviction use.",
        "verification_status: unverified",
    ]


def supplement_research_items(supplement: LogicSupplement) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    items.extend(
        {
            "item_type": "tracking_metric",
            "content": item,
            "status": "pending",
            "source_note": "system_logic_supplement",
        }
        for item in supplement.tracking_metrics
    )
    items.extend(
        {
            "item_type": "exit_condition",
            "content": item,
            "status": "pending",
            "source_note": "system_logic_supplement",
        }
        for item in supplement.exit_conditions
    )
    items.extend(
        {
            "item_type": "verification_note",
            "content": item,
            "status": "unverified",
            "source_note": "system_logic_supplement",
        }
        for item in supplement.verification_notes
    )
    return items


def fallback_research_items(name: str) -> list[dict[str, object]]:
    return [
        {
            "item_type": "verification_note",
            "content": (
                f"{name}: collect latest revenue, net profit, OPM, ROE, PE/PB, free cash flow, and leverage; "
                "verify core financial numbers against at least two independent sources before using them."
            ),
            "status": "unverified",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {
                "framework": "Step 1",
                "dimension": "financial_data_and_valuation",
                "required_sources": 2,
            },
        },
        {
            "item_type": "verification_note",
            "content": (
                f"{name}: collect industry TAM/growth, market share trend, competitors, cycle position, "
                "and entry barriers; mark unverified figures explicitly until source quality is checked."
            ),
            "status": "unverified",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "Step 1", "dimension": "industry_and_competition"},
        },
        {
            "item_type": "verification_note",
            "content": (
                f"{name}: review latest company news, strategy changes, management changes, M&A, "
                "analyst sentiment, and user-specific concerns from the original note."
            ),
            "status": "unverified",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "Step 1", "dimension": "latest_dynamics_and_management"},
        },
        {
            "item_type": "tracking_metric",
            "content": (
                f"{name}: track whether the original thesis is improving or deteriorating through 3C signals "
                "(cycle position, key change, certainty) and the most relevant 5M operating metrics."
            ),
            "status": "pending",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "3C-5M", "dimension": "thesis_tracking"},
        },
        {
            "item_type": "tracking_metric",
            "content": (
                f"{name}: track price/return, moving-average breaks, valuation sentiment, and missing price data "
                "as daily 3D/3T risk signals."
            ),
            "status": "pending",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "3D-3T", "dimension": "price_and_sentiment"},
        },
        {
            "item_type": "exit_condition",
            "content": (
                f"{name}: if verified data contradicts the original opening thesis or shows the key 3C change "
                "has reversed, mark this item contradicted and run a check."
            ),
            "status": "pending",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "3C", "dimension": "thesis_invalidated"},
        },
        {
            "item_type": "exit_condition",
            "content": (
                f"{name}: if price action confirms thesis failure, for example a decisive moving-average break "
                "or configured drawdown/stop-loss threshold, trigger exit review."
            ),
            "status": "pending",
            "source_note": "local 3C-5M-3D-3T fallback",
            "metadata": {"framework": "3D-3T", "dimension": "price_exit"},
        },
    ]


def extract_probe_terms(content: str) -> list[str]:
    terms: list[str] = []
    patterns = [
        r"\b\d{6}(?:\.(?:SZ|SH))?\b",
        r"\b\d{1,5}\.HK\b",
        r"\b[A-Z]{1,5}(?:\.US|=F)?\b",
        r"[\u4e00-\u9fffA-Za-z0-9\-]{2,20}",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, content, flags=re.IGNORECASE):
            term = match.group(0).strip("，。；;：:（）()[]【】")
            if term and term not in terms:
                terms.append(term)
    return terms


def detect_direction(content: str) -> Direction:
    lowered = content.lower()
    if any(word in lowered for word in SHORT_WORDS):
        return Direction.SHORT
    if any(word in lowered for word in LONG_WORDS):
        return Direction.LONG
    return Direction.NEUTRAL


def is_close_action(content: str) -> bool:
    lowered = content.lower()
    return any(word in lowered for word in EXIT_WORDS)


def is_extracted_close_action(signal: ExtractedSignal, source_logic: str, original_content: str) -> bool:
    action = (signal.action or "").strip().lower()
    if action == "close":
        return True
    return is_close_action(source_logic) or is_close_action(original_content)


def score_tracking_logic(content: str) -> float:
    score = 0
    if any(word in content.lower() for word in LONG_WORDS + SHORT_WORDS + EXIT_WORDS):
        score += 2
    score += min(4, sum(1 for word in LOGIC_WORDS if word in content))
    if len(content) > 120:
        score += 1
    if len(content) > 300:
        score += 1
    if re.search(r"\d+日线|跌破|突破|同比|环比|毛利率|ROE|PE|PB|库存|价格|订单|营收|利润", content, re.I):
        score += 2
    return float(min(score, 10))


def parse_weight_hints(content: str, resolutions) -> dict[str, float]:
    weights: dict[str, float] = {}
    normalized_content = content.replace("％", "%")

    for resolution in resolutions:
        instrument = resolution.instrument
        keys = [instrument.symbol, instrument.name, instrument.provider_symbol, *instrument.aliases]
        for key in keys:
            pattern = rf"{re.escape(key)}[^\d%]{{0,16}}(\d+(?:\.\d+)?)\s*%"
            match = re.search(pattern, normalized_content, flags=re.IGNORECASE)
            if match:
                weights[instrument.symbol] = float(match.group(1)) / 100
                break

    if len(weights) == len(resolutions):
        return normalize_weights(weights)

    percentages = [float(value) / 100 for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", normalized_content)]
    if len(percentages) == len(resolutions):
        return normalize_weights(
            {
                resolution.instrument.symbol: percentages[index]
                for index, resolution in enumerate(resolutions)
            }
        )

    return normalize_weights(weights)


def should_treat_as_portfolio(content: str, resolutions, weights: dict[str, float] | None = None) -> bool:
    if len(resolutions) < 2:
        return False
    lowered = content.lower()
    if any(word in lowered for word in PORTFOLIO_WORDS):
        return True
    parsed_weights = weights if weights is not None else parse_weight_hints(content, resolutions)
    return len(parsed_weights) == len(resolutions)


def normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(value for value in weights.values() if value > 0)
    if not total:
        return weights
    if abs(total - 1) <= 0.02:
        return weights
    return {key: value / total for key, value in weights.items()}


def resolve_weight_for_instrument(weights: dict[str, float], instrument, default_weight: float) -> float:
    for key in [instrument.symbol, instrument.provider_symbol, instrument.name, *instrument.aliases]:
        if key in weights:
            return weights[key]
    return default_weight


def summarize_source_logic(content: str) -> str:
    stripped = re.sub(r"\s+", " ", content.lstrip("\ufeff")).strip()
    return stripped[:1200]


def build_system_logic(name: str, direction: Direction) -> str:
    side = "做空" if direction == Direction.SHORT else "做多/观察"
    return (
        f"系统补充逻辑（{side}）：围绕 {name} 建立 3C-5M-3D-3T 跟踪框架。"
        "先按三轮信息采集法补齐证据：第一轮财务与估值，第二轮行业格局与竞争，第三轮最新动态与管理层；"
        "核心财务数据必须至少由两个独立来源交叉验证，无法验证的数据保持 unverified。"
        "3C：跟踪行业周期位置、关键变化是否兑现、变化确定性是否提升或下降。"
        "5M：跟踪市场空间、份额变化、经营利润率、商业模式现金流质量、管理层执行。"
        "3D：跟踪 ROE/PB 匹配度、外延变化催化、情绪与估值分位。"
        "3T：短期看催化和风险事件，中期看季度数据兑现，长期看行业趋势和复利能力。"
        "若原始开仓假设被财报、行业价格、份额或价格行为证伪，则标记 exit_signal 或 needs_review。"
    )


def direction_label(direction: Direction) -> str:
    if direction == Direction.LONG:
        return "做多"
    if direction == Direction.SHORT:
        return "做空"
    return "观察"
