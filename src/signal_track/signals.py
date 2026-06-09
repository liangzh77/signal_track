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
TRACK_WORDS = ("开仓", "建仓", "观察", "跟踪", "关注", "open", "track", "watch")
CONDITIONAL_EXIT_PATTERNS = (
    r"\b(?:exit|close)\s+if\b",
    r"\bif\b[^.;。；\n]{0,100}\b(?:exit|close)\b",
    r"(?:平仓|退出|止盈|止损)条件",
    r"(?:如果|若|如)[^。；;\n]{0,80}(?:平仓|退出|止盈|止损)",
    r"(?:跌破|突破|低于|高于)[^。；;\n]{0,80}(?:平仓|退出|止盈|止损)",
)


@dataclass(frozen=True)
class IngestResult:
    raw_input_id: int
    project_ids: list[int]
    resolved_symbols: list[str]
    logic_score: float
    system_logic_added: bool
    input_action: str


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
        system_logic_added = False

        if is_close_action(content) and resolutions:
            closed_project_ids = self._close_existing_projects(source_id, content, resolutions, logic_score)
            resolved_symbols = [resolution.instrument.symbol for resolution in resolutions]
            if closed_project_ids:
                return self._result(
                    raw_input_id=raw_input_id,
                    project_ids=closed_project_ids,
                    resolved_symbols=resolved_symbols,
                    logic_score=logic_score,
                    system_logic_added=False,
                    input_action="close",
                )
            return self._result(
                raw_input_id=raw_input_id,
                project_ids=[],
                resolved_symbols=resolved_symbols,
                logic_score=logic_score,
                system_logic_added=False,
                input_action="close_unmatched",
            )

        weights = weights or parse_weight_hints(content, resolutions)
        as_portfolio = as_portfolio or should_treat_as_portfolio(content, resolutions, weights)
        updated_project_ids = (
            self._append_existing_portfolio_updates(source_id, content, resolutions, direction, logic_score)
            if as_portfolio
            else self._append_existing_project_updates(source_id, content, resolutions, direction, logic_score)
        )
        if updated_project_ids:
            new_project_ids: list[int] = []
            if not as_portfolio and has_tracking_intent(content, direction, as_portfolio):
                new_project_ids = self._create_missing_single_projects(
                    source_id,
                    raw_input_id,
                    content,
                    resolutions,
                    direction,
                    logic_score,
                    needs_review,
                    updated_project_ids,
                )
            return self._result(
                raw_input_id=raw_input_id,
                project_ids=sorted(set(updated_project_ids + new_project_ids)),
                resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
                logic_score=logic_score,
                system_logic_added=bool(new_project_ids and needs_review),
                input_action="mixed" if new_project_ids else "update",
            )

        resolved_unresolved_ids = self._resolve_unresolved_project(
            source_id,
            raw_input_id,
            content,
            resolutions,
            direction,
            logic_score,
            needs_review,
            as_portfolio,
            weights,
        )
        if resolved_unresolved_ids:
            resolved_unresolved_needs_system_logic = needs_review or (
                as_portfolio and not has_complete_weights(weights or {}, resolutions)
            )
            return self._result(
                raw_input_id=raw_input_id,
                project_ids=resolved_unresolved_ids,
                resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
                logic_score=logic_score,
                system_logic_added=resolved_unresolved_needs_system_logic,
                input_action="update",
            )

        if not has_tracking_intent(content, direction, as_portfolio):
            return self._result(
                raw_input_id=raw_input_id,
                project_ids=[],
                resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
                logic_score=logic_score,
                system_logic_added=False,
                input_action="none",
            )

        system_logic_added = needs_review

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
            return self._result(raw_input_id, [project_id], [], logic_score, True, "track")

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

        return self._result(
            raw_input_id=raw_input_id,
            project_ids=project_ids,
            resolved_symbols=[resolution.instrument.symbol for resolution in resolutions],
            logic_score=logic_score,
            system_logic_added=system_logic_added,
            input_action="track",
        )

    def _result(
        self,
        raw_input_id: int,
        project_ids: list[int],
        resolved_symbols: list[str],
        logic_score: float,
        system_logic_added: bool,
        input_action: str,
    ) -> IngestResult:
        self.repo.update_raw_input_metadata(
            raw_input_id,
            {
                "project_ids": project_ids,
                "resolved_symbols": resolved_symbols,
                "logic_score": logic_score,
                "system_logic_added": system_logic_added,
                "input_action": input_action,
            },
        )
        return IngestResult(
            raw_input_id=raw_input_id,
            project_ids=project_ids,
            resolved_symbols=resolved_symbols,
            logic_score=logic_score,
            system_logic_added=system_logic_added,
            input_action=input_action,
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
        input_actions: list[str] = []

        for signal in extraction.signals:
            resolutions = self._resolve_extracted_signal(signal)
            direction = Direction(signal.direction)
            logic_score = max(0.0, min(10.0, signal.logic_score))
            needs_review = logic_score < 6 or extraction.needs_review
            source_logic = signal.source_logic or summarize_source_logic(original_content)
            if signal.observation_logic:
                source_logic = f"{source_logic}\n\n观察逻辑：{signal.observation_logic}"

            if is_noop_action(signal):
                resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                input_actions.append("none")
                continue

            if is_extracted_close_action(signal, source_logic, original_content):
                resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                if resolutions:
                    closed_project_ids = self._close_existing_projects(source_id, source_logic, resolutions, logic_score)
                    if closed_project_ids:
                        project_ids.extend(closed_project_ids)
                        input_actions.append("close")
                    else:
                        input_actions.append("close_unmatched")
                else:
                    input_actions.append("close_unmatched")
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
                    input_actions.append("update")
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
                    new_project_ids = self._create_missing_single_projects(
                        source_id,
                        raw_input_id,
                        source_logic,
                        resolutions,
                        direction,
                        logic_score,
                        needs_review,
                        updated_project_ids,
                    )
                    project_ids.extend(updated_project_ids)
                    project_ids.extend(new_project_ids)
                    resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                    system_logic_added = system_logic_added or bool(new_project_ids and needs_review)
                    input_actions.append("mixed" if new_project_ids else "update")
                    continue

            resolved_unresolved_ids = self._resolve_unresolved_project(
                source_id,
                raw_input_id,
                source_logic,
                resolutions,
                direction,
                logic_score,
                needs_review,
                signal.is_portfolio,
                signal.weights,
            )
            if resolved_unresolved_ids:
                project_ids.extend(resolved_unresolved_ids)
                resolved_symbols.extend(resolution.instrument.symbol for resolution in resolutions)
                resolved_unresolved_needs_system_logic = needs_review or (
                    signal.is_portfolio and not has_complete_weights(signal.weights or {}, resolutions)
                )
                system_logic_added = system_logic_added or resolved_unresolved_needs_system_logic
                input_actions.append("update")
                continue

            system_logic_added = system_logic_added or needs_review

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
                input_actions.append("track")
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
            input_actions.append("track")

        average_score = (
            sum(signal.logic_score for signal in extraction.signals) / len(extraction.signals)
            if extraction.signals
            else 0.0
        )
        return self._result(
            raw_input_id=raw_input_id,
            project_ids=project_ids,
            resolved_symbols=resolved_symbols,
            logic_score=round(average_score, 2),
            system_logic_added=system_logic_added,
            input_action=collapse_input_actions(input_actions),
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

    def _close_existing_projects(self, source_id: int, content: str, resolutions, logic_score: float) -> list[int]:
        closed_ids: list[int] = []
        closed_at = date.today().isoformat()
        close_symbols = {resolution.instrument.symbol for resolution in resolutions}
        candidate_ids: set[int] = set()
        for resolution in resolutions:
            for project_id in self.repo.find_active_project_ids_by_source_symbol(
                source_id,
                resolution.instrument.symbol,
            ):
                candidate_ids.add(project_id)
        for project_id in sorted(candidate_ids):
            leg_symbols = {str(leg["symbol"]) for leg in self.repo.list_project_legs(project_id)}
            if len(leg_symbols) > 1 and leg_symbols != close_symbols:
                continue
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

    def _create_missing_single_projects(
        self,
        source_id: int,
        raw_input_id: int,
        content: str,
        resolutions,
        direction: Direction,
        logic_score: float,
        needs_review: bool,
        existing_project_ids: list[int],
    ) -> list[int]:
        updated_symbols = self._project_leg_symbols(existing_project_ids)
        project_ids: list[int] = []
        for resolution in resolutions:
            if resolution.instrument.symbol in updated_symbols:
                continue
            project_ids.append(
                self._create_single_project(
                    source_id,
                    raw_input_id,
                    content,
                    resolution,
                    direction,
                    logic_score,
                    needs_review,
                )
            )
        return project_ids

    def _project_leg_symbols(self, project_ids: list[int]) -> set[str]:
        symbols: set[str] = set()
        for project_id in project_ids:
            symbols.update(str(leg["symbol"]) for leg in self.repo.list_project_legs(project_id))
        return symbols

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

    def _resolve_unresolved_project(
        self,
        source_id: int,
        raw_input_id: int,
        content: str,
        resolutions,
        direction: Direction,
        logic_score: float,
        needs_review: bool,
        as_portfolio: bool,
        weights: dict[str, float] | None,
    ) -> list[int]:
        if not resolutions:
            return []
        if len(resolutions) > 1 and not as_portfolio:
            return []

        direction_filter = None if direction == Direction.NEUTRAL else direction.value
        candidate_ids = self.repo.find_unresolved_project_ids_by_source(source_id, direction=direction_filter)
        if not candidate_ids:
            return []

        project_id = candidate_ids[0]
        if self.repo.list_project_legs(project_id):
            return []
        project = self.repo.get_project_row(project_id)
        effective_direction = direction
        if direction == Direction.NEUTRAL and project and project["direction"] in {Direction.LONG.value, Direction.SHORT.value}:
            effective_direction = Direction(project["direction"])

        weights = weights or {}
        default_weight = round(1 / len(resolutions), 6)
        weights_complete = True
        effective_weights: dict[str, float] = {}
        weight_needs_review = False
        if as_portfolio:
            weights_complete = has_complete_weights(weights, resolutions)
            effective_weights = normalize_weights(weights) if weights_complete else {}
            weight_needs_review = not weights_complete

        for resolution in resolutions:
            instrument_id = self.repo.upsert_instrument(resolution.instrument)
            weight = (
                resolve_weight_for_instrument(effective_weights, resolution.instrument, default_weight)
                if as_portfolio
                else 1.0
            )
            self.repo.add_project_leg(project_id, instrument_id, effective_direction.value, weight)

        if as_portfolio:
            title = "Portfolio tracking: " + " / ".join(resolution.instrument.name for resolution in resolutions)
            project_metadata = {"portfolio": True}
        else:
            title = f"{resolutions[0].instrument.name} {direction_label(effective_direction)}跟踪"
            project_metadata = {
                "resolution_confidence": resolutions[0].confidence,
                "resolution_reason": resolutions[0].reason,
            }
        project_metadata.update(
            {
                "raw_extract_status": "resolved_later",
                "resolved_by_raw_input_id": raw_input_id,
                "resolved_symbols": [resolution.instrument.symbol for resolution in resolutions],
            }
        )

        system_logic_required = needs_review or weight_needs_review
        review_required = weight_needs_review
        self.repo.update_tracking_project_details(
            project_id,
            title=title,
            status=(ProjectStatus.NEEDS_REVIEW if review_required else ProjectStatus.ACTIVE).value,
            direction=effective_direction.value,
            logic_score=logic_score,
            needs_review=False,
            weight_needs_review=weight_needs_review,
            metadata=project_metadata,
        )
        self.repo.add_logic_block(project_id, "source_update", summarize_source_logic(content), logic_score / 10, [content[:240]])
        if system_logic_required:
            self._add_system_logic_block(
                project_id,
                title,
                effective_direction,
                content,
                [resolution.instrument for resolution in resolutions],
                0.62,
            )
        return [project_id]

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
            status=ProjectStatus.ACTIVE.value,
            direction=direction.value,
            entry_date=date.today().isoformat(),
            logic_score=logic_score,
            needs_review=False,
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
        weights_complete = has_complete_weights(weights, resolutions)
        effective_weights = normalize_weights(weights) if weights_complete else {}
        weight_needs_review = not weights_complete
        system_logic_required = needs_review or weight_needs_review
        review_required = weight_needs_review
        title = "组合跟踪：" + " / ".join(resolution.instrument.name for resolution in resolutions)
        project_id = self.repo.create_tracking_project(
            title=title,
            source_id=source_id,
            raw_input_id=raw_input_id,
            status=(ProjectStatus.NEEDS_REVIEW if review_required else ProjectStatus.ACTIVE).value,
            direction=direction.value,
            entry_date=date.today().isoformat(),
            logic_score=logic_score,
            needs_review=False,
            weight_needs_review=weight_needs_review,
            metadata={"portfolio": True},
        )
        for resolution in resolutions:
            instrument_id = self.repo.upsert_instrument(resolution.instrument)
            weight = resolve_weight_for_instrument(effective_weights, resolution.instrument, default_weight)
            self.repo.add_project_leg(project_id, instrument_id, direction.value, weight)
        self.repo.add_logic_block(project_id, "source_logic", summarize_source_logic(content), logic_score / 10, [content[:240]])
        if system_logic_required:
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
        (r"\b\d{6}(?:\.(?:SZ|SH))?\b", re.IGNORECASE),
        (r"\b\d{1,5}\.HK\b", re.IGNORECASE),
        (r"\b[A-Z]{1,4}\d{3,4}\.(?:SHF|DCE|CZC|CFX|INE|GFE)\b", re.IGNORECASE),
        (r"\b[A-Z]{1,4}\.(?:SHF|DCE|CZC|CFX|INE|GFE)\b", re.IGNORECASE),
        (r"\b\d{4,5}\b", 0),
        (r"\b[A-Z]{1,5}(?:\.US|=F)?\b", 0),
        (r"[\u4e00-\u9fffA-Za-z0-9\-]{2,20}", 0),
    ]
    for pattern, flags in patterns:
        for match in re.finditer(pattern, content, flags=flags):
            term = match.group(0).strip("，。；;：:（）()[]【】")
            if term and should_probe_term(term) and term not in terms:
                terms.append(term)
    return terms


def should_probe_term(term: str) -> bool:
    upper = term.upper()
    cn_future_exchange_suffixes = {"SHF", "DCE", "CZC", "CFX", "INE", "GFE"}
    if upper in cn_future_exchange_suffixes:
        return False
    financial_metric_terms = {
        "PE",
        "PB",
        "PS",
        "PEG",
        "ROE",
        "ROA",
        "ROIC",
        "OPM",
        "TAM",
        "EPS",
        "FCF",
        "EBIT",
        "EBITDA",
    }
    if upper in financial_metric_terms:
        return False
    non_instrument_terms = {
        "HK",
        "US",
        "SZ",
        "SH",
        "LONG",
        "SHORT",
        "BUY",
        "SELL",
        "HOLD",
        "WATCH",
        "TRACK",
        "CLOSE",
        "EXIT",
        "PORTFOLIO",
        "BASKET",
        "PAIR",
        "TRADE",
        "THESIS",
    }
    if upper in non_instrument_terms:
        return False
    if re.search(r"[\u4e00-\u9fff]", term):
        return True
    if re.fullmatch(r"\d{6}(?:\.(?:SZ|SH))?", term, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"\d{1,5}\.HK", term, flags=re.IGNORECASE):
        return True
    if re.fullmatch(r"\d{4,5}", term):
        return not is_likely_year(term)
    if re.fullmatch(r"[A-Z]{1,4}\d{3,4}\.(?:SHF|DCE|CZC|CFX|INE|GFE)", term):
        return True
    if re.fullmatch(r"[A-Z]{1,4}\.(?:SHF|DCE|CZC|CFX|INE|GFE)", term):
        return True
    if re.fullmatch(r"[A-Z]{1,5}(?:\.US|=F)?", term):
        return True
    return bool(re.fullmatch(r"[A-Z][A-Za-z]{2,19}", term))


def is_likely_year(term: str) -> bool:
    return bool(re.fullmatch(r"(?:19|20)\d{2}", term))


def detect_direction(content: str) -> Direction:
    lowered = content.lower()
    if any(word in lowered for word in SHORT_WORDS):
        return Direction.SHORT
    if any(word in lowered for word in LONG_WORDS):
        return Direction.LONG
    return Direction.NEUTRAL


def is_close_action(content: str) -> bool:
    lowered = content.lower()
    if not any(word in lowered for word in EXIT_WORDS):
        return False
    if has_entry_context(content) and has_conditional_exit_context(content):
        return False
    return True


def has_entry_context(content: str) -> bool:
    lowered = content.lower()
    return any(word in lowered for word in LONG_WORDS + SHORT_WORDS + TRACK_WORDS)


def has_conditional_exit_context(content: str) -> bool:
    lowered = content.lower()
    return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in CONDITIONAL_EXIT_PATTERNS)


def is_extracted_close_action(signal: ExtractedSignal, source_logic: str, original_content: str) -> bool:
    action = (signal.action or "").strip().lower()
    if action == "close":
        return True
    if action in {"open", "none"}:
        return False
    return is_close_action(source_logic) or is_close_action(original_content)


def is_noop_action(signal: ExtractedSignal) -> bool:
    return (signal.action or "").strip().lower() == "none"


def collapse_input_actions(actions: list[str]) -> str:
    unique = [action for action in dict.fromkeys(actions) if action]
    if not unique:
        return "none"
    if len(unique) == 1:
        return unique[0]
    return "mixed"


def has_tracking_intent(content: str, direction: Direction, as_portfolio: bool = False) -> bool:
    lowered = content.lower()
    return direction != Direction.NEUTRAL or as_portfolio or any(word in lowered for word in TRACK_WORDS)


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
            pattern = (
                rf"{re.escape(key)}"
                rf"(?P<label>[\s,，:：;；()（）\-—]*"
                rf"(?:(?:权重|占比|仓位|weight|allocation)[\s,，:：;；()（）\-—]*)?)"
                rf"(?P<weight>\d+(?:\.\d+)?)\s*%"
            )
            match = re.search(pattern, normalized_content, flags=re.IGNORECASE)
            if match:
                weights[instrument.symbol] = float(match.group("weight")) / 100
                break
            ratio_pattern = (
                rf"{re.escape(key)}"
                rf"[\s,，:：;；()（）\-—]*"
                rf"(?:权重|占比|仓位|weight|allocation)[\s,，:：;；()（）\-—]*"
                rf"(?P<weight>\d+(?:\.\d+)?)\b(?!\s*%)"
            )
            ratio_match = re.search(ratio_pattern, normalized_content, flags=re.IGNORECASE)
            if ratio_match:
                weights[instrument.symbol] = float(ratio_match.group("weight"))
                break

    if len(weights) == len(resolutions):
        return normalize_weights(weights)

    percentages = [float(value) / 100 for value in re.findall(r"(\d+(?:\.\d+)?)\s*%", normalized_content)]
    if len(percentages) == len(resolutions) and has_weight_context(normalized_content):
        return normalize_weights(
            {
                resolution.instrument.symbol: percentages[index]
                for index, resolution in enumerate(resolutions)
            }
        )

    ordered_values = parse_ordered_weight_values(normalized_content, len(resolutions))
    if ordered_values:
        return normalize_weights(
            {
                resolution.instrument.symbol: ordered_values[index]
                for index, resolution in enumerate(resolutions)
            }
        )

    return normalize_weights(weights)


def has_weight_context(content: str) -> bool:
    lowered = content.lower()
    return any(word in lowered for word in ("权重", "占比", "仓位", "weight", "allocation"))


def parse_ordered_weight_values(content: str, count: int) -> list[float]:
    if count <= 0 or not has_weight_context(content):
        return []
    lowered = content.lower()
    markers = ("权重", "占比", "仓位", "weights", "weight", "allocation")
    positions = [lowered.find(marker) for marker in markers if lowered.find(marker) >= 0]
    if not positions:
        return []
    tail = content[min(positions) : min(positions) + 160]
    values = [
        float(value)
        for value in re.findall(r"(?<![A-Za-z0-9.])(\d+(?:\.\d+)?)(?![A-Za-z0-9.])", tail)
    ]
    if len(values) < count:
        return []
    return values[:count]


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


def has_complete_weights(weights: dict[str, float], resolutions) -> bool:
    if not weights or not resolutions:
        return False
    for resolution in resolutions:
        if resolve_weight_for_instrument(weights, resolution.instrument, -1) <= 0:
            return False
    return True


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
