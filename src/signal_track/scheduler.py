from __future__ import annotations

from dataclasses import dataclass

from .checker import DailyChecker
from .daily_evaluator import DailyLogicEvaluator
from .dashboard import render_dashboard
from .db import Repository
from .publisher import DemoPublisher, PublishResult, publish_payload
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


def scheduler_job_summaries(scheduler: object) -> list[dict[str, str | None]]:
    get_jobs = getattr(scheduler, "get_jobs", None)
    if not callable(get_jobs):
        return []
    summaries = []
    for job in get_jobs():
        next_run_time = getattr(job, "next_run_time", None)
        summaries.append(
            {
                "id": str(getattr(job, "id", "")),
                "trigger": str(getattr(job, "trigger", "")),
                "next_run_time": next_run_time.isoformat() if next_run_time else None,
            }
        )
    return summaries


def execute_daily_check(
    repo: Repository,
    provider: MarketDataProvider | None = None,
    evaluator: DailyLogicEvaluator | None = None,
    publish_url: str | None = None,
    api_key: str | None = None,
) -> int:
    checked = DailyChecker(repo, provider, evaluator=evaluator).run()
    if publish_url and api_key:
        record_scheduled_publish(repo, publish_url, api_key)
    return checked


def record_scheduled_publish(repo: Repository, publish_url: str, api_key: str) -> None:
    title = "Signal Track 投资信号看板"
    metadata = {"job": "daily_check"}
    try:
        result = DemoPublisher(publish_url, api_key).publish(
            title=title,
            html=render_dashboard(repo),
            feature="每日检查后自动发布",
        )
    except Exception as exc:
        result = PublishResult(False, None, str(exc))
        metadata["exception_type"] = type(exc).__name__

    payload = publish_payload(result, publish_url)
    metadata.update(
        {
            "ok": payload["ok"],
            "publish_url": payload["publish_url"],
            "error": payload["error"],
        }
    )
    repo.record_publish_event(
        title=title,
        url=payload["url"] or publish_url,
        status_code=payload["status_code"],
        response_body=payload["response_body"],
        metadata=metadata,
    )
