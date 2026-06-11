from __future__ import annotations

import json
from datetime import date, timedelta

from .analytics import instrument_from_leg_row, project_performance
from .daily_evaluator import DailyLogicEvaluator
from .db import Repository
from .market_data import MarketDataService
from .providers.base import MarketDataProvider
from .rules import evaluate_project_rules


class DailyChecker:
    def __init__(
        self,
        repo: Repository,
        provider: MarketDataProvider | None = None,
        evaluator: DailyLogicEvaluator | None = None,
    ):
        self.repo = repo
        self.provider = provider
        self.evaluator = evaluator

    def run(self, check_date: date | None = None) -> int:
        current = check_date or date.today()
        current_date = current.isoformat()
        refresh_errors_by_project: dict[int, list[str]] = {}
        for project_id in self.repo.list_price_refresh_project_ids(current_date):
            refresh_errors = self._refresh_prices(project_id, current)
            if refresh_errors:
                refresh_errors_by_project[project_id] = refresh_errors
        project_ids = self.repo.list_active_project_ids()
        for project_id in project_ids:
            performance = project_performance(self.repo, project_id, current)
            conclusion = "watch"
            triggered_rules: list[str] = list(refresh_errors_by_project.get(project_id, []))
            project = self.repo.get_project_row(project_id)
            existing_exit_signal = bool(project and project["status"] == "exit_signal")
            logic_blocks = self.repo.list_logic_blocks(project_id)

            if existing_exit_signal:
                conclusion = "exit_signal"
                triggered_rules.append("Existing exit signal remains open until the project is closed.")

            project_review_rules = project_level_review_rules(project, logic_blocks) if project else []
            if project_review_rules and not existing_exit_signal:
                conclusion = "needs_review"
                triggered_rules.extend(project_review_rules)
                self.repo.update_project_status(project_id, "needs_review", needs_review=True)

            if triggered_rules and not existing_exit_signal:
                conclusion = "needs_review"
                self.repo.update_project_status(project_id, "needs_review", needs_review=True)

            if not performance.legs:
                triggered_rules.append("No resolved instrument; cannot refresh prices or calculate returns.")
                if not existing_exit_signal:
                    conclusion = "needs_review"
                    self.repo.update_project_status(project_id, "needs_review", needs_review=True)
            elif performance.missing_price_symbols:
                triggered_rules.append("缺少行情数据：" + ", ".join(performance.missing_price_symbols))
                if not existing_exit_signal:
                    conclusion = "needs_review"
                    self.repo.update_project_status(project_id, "needs_review", needs_review=True)
            elif performance.return_pct is not None:
                default_close_reason = default_close_rule_hit(project, performance, current) if project else None
                if default_close_reason:
                    conclusion = "closed"
                    triggered_rules.append(default_close_reason)
                    self.repo.close_project(
                        project_id,
                        current_date,
                        metadata={"closed_by_rule": True, "close_reason": default_close_reason},
                    )

            rule_hits = [] if conclusion == "closed" else evaluate_project_rules(self.repo, project_id, performance, current)
            if rule_hits:
                conclusion = "closed"
                triggered_rules.extend(hit.message for hit in rule_hits)
                self.repo.close_project(
                    project_id,
                    current_date,
                    metadata={"closed_by_rule": True, "close_reason": "; ".join(hit.message for hit in rule_hits)},
                )

            research_conclusion, research_rules = (
                (None, []) if conclusion == "closed" else evaluate_research_item_statuses(
                    self.repo.list_research_items(project_id=project_id)
                )
            )
            if research_rules:
                triggered_rules.extend(research_rules)
                if research_conclusion == "exit_signal":
                    conclusion = "exit_signal"
                    self.repo.update_project_status(project_id, "exit_signal", needs_review=True)
                elif conclusion != "exit_signal":
                    conclusion = "needs_review"
                    self.repo.update_project_status(project_id, "needs_review", needs_review=True)

            summary = build_summary(performance)
            evaluation = None if conclusion == "closed" else self._evaluate_logic(project_id, performance, current)
            if evaluation:
                summary = merge_summary(summary, evaluation.summary)
                triggered_rules.extend(evaluation.triggered_rules)
                if evaluation.conclusion == "exit_signal" and conclusion != "exit_signal":
                    conclusion = "exit_signal"
                    self.repo.update_project_status(project_id, "exit_signal", needs_review=True)
                elif conclusion not in {"exit_signal", "needs_review"}:
                    conclusion = evaluation.conclusion
                    if evaluation.conclusion == "needs_review":
                        self.repo.update_project_status(project_id, "needs_review", needs_review=True)
            self._clear_transient_review_if_resolved(project_id, conclusion)
            self.repo.add_daily_check(
                project_id=project_id,
                check_date=current_date,
                conclusion=conclusion,
                summary=summary,
                triggered_rules=triggered_rules,
            )
        return len(project_ids)

    def _refresh_prices(self, project_id: int, current: date) -> list[str]:
        if not self.provider:
            return []
        service = MarketDataService(self.repo, self.provider)
        project = self.repo.get_project_row(project_id)
        if not project:
            return []
        start = date.fromisoformat(project["entry_date"] or project["created_at"][:10]) - timedelta(days=120)
        errors: list[str] = []
        for leg in self.repo.list_project_legs(project_id):
            instrument = instrument_from_leg_row(leg)
            try:
                service.fetch_and_store(instrument, start, current)
            except Exception as exc:
                latest_bar = self.repo.get_latest_price_on_or_before(instrument.id or int(leg["instrument_id"]), current.isoformat())
                if latest_bar and price_bar_is_recent(str(latest_bar["bar_date"]), current):
                    continue
                errors.append(f"行情刷新失败：{instrument.symbol} - {exc}")
        return errors

    def _evaluate_logic(self, project_id: int, performance, current: date):
        if not self.evaluator:
            return None
        project = self.repo.get_project_row(project_id)
        if not project:
            return None
        try:
            return self.evaluator.evaluate(
                project=project,
                logic_blocks=self.repo.list_logic_blocks(project_id),
                research_items=self.repo.list_research_items(project_id=project_id),
                performance=performance,
                previous_checks=self.repo.list_daily_checks(project_id=project_id, limit=5),
                check_date=current,
            )
        except Exception:
            return None

    def _clear_transient_review_if_resolved(self, project_id: int, conclusion: str) -> None:
        if conclusion not in {"watch", "hold"}:
            return
        project = self.repo.get_project_row(project_id)
        if not project or project["status"] != "needs_review":
            return
        if has_project_level_review_reason(project, self.repo.list_logic_blocks(project_id)):
            return
        self.repo.update_project_status(project_id, "active", needs_review=False)

def merge_summary(base: str, evaluator_summary: str) -> str:
    if not evaluator_summary:
        return base
    if not base:
        return evaluator_summary
    return f"{base}\n\n逻辑评估：{evaluator_summary}"


def evaluate_research_item_statuses(items) -> tuple[str | None, list[str]]:
    conclusion = None
    rules: list[str] = []
    for item in items:
        if item["status"] != "contradicted":
            continue
        message = f"研究验证项被证伪：{item['item_type']} - {item['content']}"
        rules.append(message)
        if item["item_type"] == "exit_condition":
            conclusion = "exit_signal"
        elif conclusion != "exit_signal":
            conclusion = "needs_review"
    return conclusion, rules


def build_summary(performance) -> str:
    if not performance.legs:
        return "检查完成，但项目尚未解析出标的，无法刷新行情或计算收益。"
    if performance.return_pct is None:
        if performance.missing_price_symbols:
            return "检查完成，但缺少行情数据，无法计算收益：" + ", ".join(performance.missing_price_symbols)
        return "检查完成，但暂无可计算收益。"
    leg_parts = []
    for leg in performance.legs:
        if leg.return_pct is None:
            leg_parts.append(f"{leg.symbol}: 缺行情")
        else:
            leg_parts.append(f"{leg.symbol}: {leg.return_pct:.2%}")
    return f"项目当前收益 {performance.return_pct:.2%}，最新日期 {performance.latest_date or '未知'}；" + "；".join(leg_parts)


def has_project_level_review_reason(project, logic_blocks: list | None = None) -> bool:
    return bool(project_level_review_rules(project, logic_blocks or []))


def price_bar_is_recent(bar_date: str, current: date, max_staleness_days: int = 7) -> bool:
    try:
        latest = date.fromisoformat(bar_date)
    except ValueError:
        return False
    return 0 <= (current - latest).days <= max_staleness_days


def default_close_rule_hit(
    project,
    performance,
    check_date: date | None = None,
    stop_loss: float = -0.20,
    trailing_drawdown: float = -0.20,
) -> str | None:
    metadata = project_metadata(project)
    if default_close_rule_disabled(metadata, check_date, performance.latest_date):
        return None
    if performance.return_pct is None:
        return None
    if performance.return_pct <= stop_loss:
        return f"默认平仓规则触发：项目收益跌至 {performance.return_pct:.2%}，低于 -20%。"
    entry_date = project["entry_date"] or project["created_at"][:10]
    post_entry_points = [(point_date, value) for point_date, value in performance.points if point_date >= entry_date]
    if not post_entry_points:
        return None
    peak_return = max(value for _, value in post_entry_points)
    latest_return = post_entry_points[-1][1]
    if peak_return <= 0:
        return None
    drawdown_from_peak = (1 + latest_return) / (1 + peak_return) - 1
    if drawdown_from_peak <= trailing_drawdown:
        return (
            "默认止盈规则触发："
            f"从最高收益 {peak_return:.2%} 回撤至 {latest_return:.2%}，"
            f"回撤 {drawdown_from_peak:.2%}。"
        )
    return None


def project_metadata(project) -> dict:
    try:
        metadata = json.loads(project["metadata"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def default_close_rule_disabled(metadata: dict, check_date: date | None, latest_date: str | None) -> bool:
    if metadata.get("default_close_rule") == "disabled":
        return True
    if metadata.get("default_close_rule") != "disabled_until":
        return False
    hold_until = str(metadata.get("hold_until") or "").strip()
    if not hold_until:
        return False
    try:
        until = date.fromisoformat(hold_until)
    except ValueError:
        return False
    current = check_date
    if current is None and latest_date:
        try:
            current = date.fromisoformat(latest_date)
        except ValueError:
            current = None
    return current is None or current <= until


def project_level_review_rules(project, logic_blocks: list | None = None) -> list[str]:
    rules: list[str] = []
    logic_blocks = logic_blocks or []
    has_system_review = any(block["logic_type"] == "system_logic" for block in logic_blocks)
    if float(project["logic_score"]) < 6 and not has_system_review:
        rules.append(
            f"Project logic score {float(project['logic_score']):.1f} is below 6; "
            "keep the project in review until automatic thesis supplementation runs."
        )
    if bool(project["weight_needs_review"]):
        rules.append("Portfolio weights need review; equal-weight tracking is provisional.")
    return rules
