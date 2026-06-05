from __future__ import annotations

from datetime import date, timedelta

from .analytics import instrument_from_leg_row, project_performance
from .db import Repository
from .market_data import MarketDataService
from .providers.base import MarketDataProvider
from .rules import evaluate_project_rules


class DailyChecker:
    def __init__(self, repo: Repository, provider: MarketDataProvider | None = None):
        self.repo = repo
        self.provider = provider

    def run(self, check_date: date | None = None) -> int:
        current = check_date or date.today()
        current_date = current.isoformat()
        project_ids = self.repo.list_active_project_ids()
        for project_id in project_ids:
            self._refresh_prices(project_id, current)
            performance = project_performance(self.repo, project_id, current)
            conclusion = "watch"
            triggered_rules: list[str] = []

            if performance.missing_price_symbols:
                conclusion = "needs_review"
                triggered_rules.append("缺少行情数据：" + ", ".join(performance.missing_price_symbols))
                self.repo.update_project_status(project_id, "needs_review", needs_review=True)
            elif performance.return_pct is not None and performance.return_pct <= -0.10:
                conclusion = "exit_signal"
                triggered_rules.append(f"项目收益回撤达到 {performance.return_pct:.2%}")
                self.repo.update_project_status(project_id, "exit_signal", needs_review=True)

            rule_hits = evaluate_project_rules(self.repo, project_id, performance, current)
            if rule_hits:
                conclusion = "exit_signal"
                triggered_rules.extend(hit.message for hit in rule_hits)
                self.repo.update_project_status(project_id, "exit_signal", needs_review=True)

            summary = build_summary(performance)
            self.repo.add_daily_check(
                project_id=project_id,
                check_date=current_date,
                conclusion=conclusion,
                summary=summary,
                triggered_rules=triggered_rules,
            )
        return len(project_ids)

    def _refresh_prices(self, project_id: int, current: date) -> None:
        if not self.provider:
            return
        service = MarketDataService(self.repo, self.provider)
        project = self.repo.get_project_row(project_id)
        if not project:
            return
        start = date.fromisoformat(project["entry_date"] or project["created_at"][:10]) - timedelta(days=120)
        for leg in self.repo.list_project_legs(project_id):
            service.fetch_and_store(instrument_from_leg_row(leg), start, current)


def build_summary(performance) -> str:
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
