from __future__ import annotations

from dataclasses import dataclass

from .checker import DailyChecker
from .daily_evaluator import DailyLogicEvaluator
from .dashboard import render_dashboard
from .db import Repository
from .publisher import DemoPublisher, extract_published_address
from .providers.base import MarketDataProvider


@dataclass(frozen=True)
class ScheduledJobs:
    scheduler: object


def build_scheduler(
    repo: Repository,
    timezone: str = "Asia/Shanghai",
    provider: MarketDataProvider | None = None,
    evaluator: DailyLogicEvaluator | None = None,
    publish_url: str | None = None,
    api_key: str | None = None,
) -> ScheduledJobs:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError as exc:
        raise RuntimeError("Install web extras first: pip install -e .[web]") from exc

    scheduler = BackgroundScheduler(timezone=timezone)

    def run_daily_check() -> None:
        execute_daily_check(repo, provider, evaluator, publish_url, api_key)

    scheduler.add_job(run_daily_check, "cron", hour=19, minute=0, id="asia_evening_daily_check")
    scheduler.add_job(run_daily_check, "cron", hour=7, minute=0, id="us_morning_daily_check")
    return ScheduledJobs(scheduler=scheduler)


def execute_daily_check(
    repo: Repository,
    provider: MarketDataProvider | None = None,
    evaluator: DailyLogicEvaluator | None = None,
    publish_url: str | None = None,
    api_key: str | None = None,
) -> int:
    checked = DailyChecker(repo, provider, evaluator=evaluator).run()
    if publish_url and api_key:
        result = DemoPublisher(publish_url, api_key).publish(
            title="Signal Track 投资信号看板",
            html=render_dashboard(repo),
            feature="每日检查后自动发布",
        )
        repo.record_publish_event(
            title="Signal Track 投资信号看板",
            url=extract_published_address(result.body) or publish_url,
            status_code=result.status_code,
            response_body=result.body,
            metadata={"ok": result.ok, "job": "daily_check"},
        )
    return checked
