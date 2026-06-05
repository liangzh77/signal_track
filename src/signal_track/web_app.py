from __future__ import annotations

from dataclasses import dataclass

from .checker import DailyChecker
from .config import Settings
from .dashboard import render_dashboard
from .db import Database, Repository
from .extraction import OpenAISignalExtractor
from .publisher import DemoPublisher
from .providers.factory import build_market_data_provider
from .resolver import InstrumentResolver, SEED_INSTRUMENTS
from .scheduler import build_scheduler
from .signals import SignalIngestor


@dataclass(frozen=True)
class IngestRequest:
    source: str
    content: str
    portfolio: bool = False


def create_app():
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import HTMLResponse
        from pydantic import BaseModel
    except ImportError as exc:
        raise RuntimeError("Install web extras first: pip install -e .[web]") from exc

    settings = Settings.from_env()
    db = Database(settings.db_path)
    db.init()
    repo = Repository(db)
    if not repo.list_instruments():
        for instrument in SEED_INSTRUMENTS:
            repo.upsert_instrument(instrument)
    scheduled_jobs = None

    class InputPayload(BaseModel):
        source: str = "manual"
        content: str
        portfolio: bool = False
        extractor: str = "auto"

    app = FastAPI(title="Signal Track", version="0.1.0")

    if settings.enable_scheduler:
        provider = build_market_data_provider(settings.daily_provider, settings)
        scheduled_jobs = build_scheduler(
            repo,
            provider=provider,
            publish_url=settings.demo_publish_url,
            api_key=settings.demo_api_key,
        )

        @app.on_event("startup")
        def start_scheduler():
            scheduled_jobs.scheduler.start()

        @app.on_event("shutdown")
        def stop_scheduler():
            scheduled_jobs.scheduler.shutdown(wait=False)

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/api/inputs")
    def ingest(payload: InputPayload):
        resolver = InstrumentResolver(repo.list_instruments())
        extraction = None
        source_name = payload.source
        if payload.extractor in {"auto", "openai"} and settings.openai_api_key:
            extraction = OpenAISignalExtractor(settings.openai_api_key, settings.openai_model).extract(
                payload.content,
                source_hint=payload.source,
            )
            if payload.source == "manual" and extraction.source_name:
                source_name = extraction.source_name
        elif payload.extractor == "openai":
            raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required for extractor=openai")
        result = SignalIngestor(repo, resolver).ingest(
            source_name=source_name,
            content=payload.content,
            as_portfolio=payload.portfolio,
            extraction=extraction,
        )
        publish_result = maybe_publish(repo, settings, "新增信息后自动发布")
        return {
            "raw_input_id": result.raw_input_id,
            "project_ids": result.project_ids,
            "resolved_symbols": result.resolved_symbols,
            "logic_score": result.logic_score,
            "system_logic_added": result.system_logic_added,
            "publish": publish_result,
        }

    @app.get("/api/projects")
    def list_projects():
        return [dict(row) for row in repo.list_project_rows()]

    @app.post("/api/checks/run")
    def run_checks():
        checked = DailyChecker(repo).run()
        publish_result = maybe_publish(repo, settings, f"每日检查完成，更新 {checked} 个项目")
        return {"checked_projects": checked, "publish": publish_result}

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(render_dashboard(repo))

    @app.post("/api/publish")
    def publish():
        if not settings.demo_publish_url or not settings.demo_api_key:
            raise HTTPException(
                status_code=503,
                detail="GO_SITES_DEMO_PUBLISH_URL and GO_SITES_DEMO_API_KEY are required",
            )
        result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
            title="Signal Track 投资信号看板",
            html=render_dashboard(repo),
        )
        repo.record_publish_event(
            title="Signal Track 投资信号看板",
            url=settings.demo_publish_url,
            status_code=result.status_code,
            response_body=result.body,
            metadata={"ok": result.ok},
        )
        if not result.ok:
            raise HTTPException(status_code=result.status_code or 502, detail=result.body)
        return {"ok": True, "status_code": result.status_code}

    return app


def maybe_publish(repo: Repository, settings: Settings, feature: str) -> dict:
    if not settings.demo_publish_url or not settings.demo_api_key:
        return {"attempted": False, "ok": False, "reason": "publish credentials not configured"}
    result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
        title="Signal Track 投资信号看板",
        html=render_dashboard(repo),
        feature=feature,
    )
    repo.record_publish_event(
        title="Signal Track 投资信号看板",
        url=settings.demo_publish_url,
        status_code=result.status_code,
        response_body=result.body,
        metadata={"ok": result.ok, "feature": feature},
    )
    return {"attempted": True, "ok": result.ok, "status_code": result.status_code}
