from __future__ import annotations

from dataclasses import dataclass

from .checker import DailyChecker
from .dashboard import render_dashboard
from .db import Repository
from .publisher import DemoPublisher
from .providers.base import MarketDataProvider


@dataclass(frozen=True)
class ScheduledJobs:
    scheduler: object


def build_scheduler(
    repo: Repository,
    timezone: str = "Asia/Shanghai",
    provider: MarketDataProvider | None = None,
    publish_url: str | None = None,
    api_key: str | None = None,
) -> ScheduledJobs:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except ImportError as exc:
        raise RuntimeError("Install web extras first: pip install -e .[web]") from exc

    scheduler = BackgroundScheduler(timezone=timezone)

    def run_daily_check() -> None:
        DailyChecker(repo, provider).run()
        if publish_url and api_key:
            result = DemoPublisher(publish_url, api_key).publish(
                title="Signal Track 投资信号看板",
                html=render_dashboard(repo),
                feature="每日检查后自动发布",
            )
            repo.record_publish_event(
                title="Signal Track 投资信号看板",
                url=publish_url,
                status_code=result.status_code,
                response_body=result.body,
                metadata={"ok": result.ok, "job": "daily_check"},
            )

    scheduler.add_job(run_daily_check, "cron", hour=19, minute=0, id="cn_hk_daily_check")
    return ScheduledJobs(scheduler=scheduler)
