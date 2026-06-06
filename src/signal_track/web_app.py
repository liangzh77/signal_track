from dataclasses import dataclass
from pathlib import Path

from .analytics import project_performance
from .checker import DailyChecker
from .config import Settings
from .daily_evaluator import build_daily_logic_evaluator
from .dashboard import render_dashboard
from .db import Database, Repository
from .extraction import OpenAISignalExtractor
from .instrument_master import InstrumentMasterService
from .logic_supplement import build_logic_supplementer
from .models import Market
from .provider_diagnostics import market_data_coverage
from .project_summary import project_summaries
from .publisher import DemoPublisher, extract_published_address
from .providers.factory import build_market_data_provider
from .resolver import InstrumentResolver, SEED_INSTRUMENTS
from .scheduler import build_scheduler
from .signals import SignalIngestor
from .source_detection import remove_source_marker_lines, resolve_source_name


@dataclass(frozen=True)
class IngestRequest:
    source: str | None
    content: str
    portfolio: bool = False


def create_app():
    try:
        from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
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
        source: str | None = None
        content: str
        portfolio: bool = False
        extractor: str = "auto"

    class CheckPayload(BaseModel):
        provider: str = "none"

    class RefreshInstrumentsPayload(BaseModel):
        provider: str = "tushare"
        market: str = "all"

    class ResearchItemUpdatePayload(BaseModel):
        status: str
        source_note: str | None = None
        metadata: dict | None = None
        run_check: bool = False
        provider: str = "none"

    app = FastAPI(title="Signal Track", version="0.1.0")

    def require_write_auth(
        authorization: str | None = Header(default=None),
        x_signal_track_key: str | None = Header(default=None),
    ) -> None:
        if not settings.signal_track_api_key:
            return
        bearer = None
        if authorization and authorization.lower().startswith("bearer "):
            bearer = authorization.split(" ", 1)[1].strip()
        if x_signal_track_key == settings.signal_track_api_key or bearer == settings.signal_track_api_key:
            return
        raise HTTPException(status_code=401, detail="Unauthorized")

    if settings.enable_scheduler:
        provider = build_market_data_provider(settings.daily_provider, settings)
        scheduled_jobs = build_scheduler(
            repo,
            provider=provider,
            evaluator=build_daily_logic_evaluator(settings.openai_api_key, settings.openai_model),
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

    @app.get("/api/market-data/coverage")
    def market_coverage(provider: str = "auto"):
        try:
            return market_data_coverage(settings, provider)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/inputs", dependencies=[Depends(require_write_auth)])
    def ingest(payload: InputPayload):
        result = ingest_content(
            repo,
            settings,
            source=payload.source,
            content=payload.content,
            portfolio=payload.portfolio,
            extractor=payload.extractor,
        )
        publish_result = maybe_publish(repo, settings, "新增信息后自动发布")
        return result_response(repo, result, publish_result)

    @app.post("/api/inputs/file", dependencies=[Depends(require_write_auth)])
    async def ingest_file(
        source: str | None = Form(None),
        portfolio: bool = Form(False),
        extractor: str = Form("auto"),
        file: UploadFile = File(...),
    ):
        attachments_dir = settings.db_path.parent / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(file.filename or "input.txt").name
        attachment_path = attachments_dir / safe_name
        content_bytes = await file.read()
        attachment_path.write_bytes(content_bytes)
        content = content_bytes.decode("utf-8", errors="replace")
        result = ingest_content(
            repo,
            settings,
            source=source,
            content=content,
            portfolio=portfolio,
            extractor=extractor,
            attachment_path=str(attachment_path),
        )
        publish_result = maybe_publish(repo, settings, "上传文件后自动发布")
        return result_response(repo, result, publish_result)

    @app.get("/api/projects")
    def list_projects():
        return [dict(row) for row in repo.list_project_rows()]

    @app.get("/api/instruments")
    def list_instruments():
        return [
            {
                "id": instrument.id,
                "symbol": instrument.symbol,
                "provider_symbol": instrument.provider_symbol,
                "name": instrument.name,
                "market": instrument.market.value,
                "asset_type": instrument.asset_type.value,
                "exchange": instrument.exchange,
                "currency": instrument.currency,
                "timezone": instrument.timezone,
                "aliases": list(instrument.aliases),
            }
            for instrument in repo.list_instruments()
        ]

    @app.post("/api/instruments/refresh", dependencies=[Depends(require_write_auth)])
    def refresh_instruments(payload: RefreshInstrumentsPayload):
        try:
            provider = build_market_data_provider(payload.provider, settings)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if provider is None:
            raise HTTPException(status_code=400, detail="A concrete provider is required")
        markets = refresh_markets(payload.market)
        results = InstrumentMasterService(repo, provider).refresh_many(markets)
        return {
            "provider": provider.name,
            "results": [
                {"market": result.market.value, "count": result.count}
                for result in results
            ],
        }

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int):
        project = repo.get_project_row(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        performance = project_performance(repo, project_id)
        return {
            "project": dict(project),
            "legs": [dict(row) for row in repo.list_project_legs(project_id)],
            "logic_blocks": [dict(row) for row in repo.list_logic_blocks(project_id)],
            "research_items": [dict(row) for row in repo.list_research_items(project_id=project_id)],
            "daily_checks": [dict(row) for row in repo.list_daily_checks(project_id=project_id)],
            "performance": {
                "return_pct": performance.return_pct,
                "latest_date": performance.latest_date,
                "points": performance.points,
                "missing_price_symbols": performance.missing_price_symbols,
                "legs": [leg.__dict__ for leg in performance.legs],
            },
        }

    @app.get("/api/research-items")
    def list_research_items(project_id: int | None = None, limit: int = 100):
        return [dict(row) for row in repo.list_research_items(project_id=project_id, limit=limit)]

    @app.patch("/api/research-items/{item_id}", dependencies=[Depends(require_write_auth)])
    def update_research_item(item_id: int, payload: ResearchItemUpdatePayload):
        if payload.status not in {"pending", "unverified", "verified", "contradicted", "ignored"}:
            raise HTTPException(status_code=400, detail="Invalid research item status")
        item = repo.update_research_item(
            item_id,
            status=payload.status,
            source_note=payload.source_note,
            metadata=payload.metadata,
        )
        if not item:
            raise HTTPException(status_code=404, detail="Research item not found")
        checked = None
        if payload.run_check:
            try:
                provider = build_market_data_provider(payload.provider, settings)
            except ValueError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            checked = DailyChecker(
                repo,
                provider,
                evaluator=build_daily_logic_evaluator(settings.openai_api_key, settings.openai_model),
            ).run()
        publish_result = maybe_publish(repo, settings, f"研究验证项更新：{payload.status}")
        return {"item": dict(item), "checked_projects": checked, "publish": publish_result}

    @app.post("/api/checks/run", dependencies=[Depends(require_write_auth)])
    def run_checks(payload: CheckPayload = Body(default=CheckPayload())):
        try:
            provider = build_market_data_provider(payload.provider, settings)
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        checked = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_logic_evaluator(settings.openai_api_key, settings.openai_model),
        ).run()
        publish_result = maybe_publish(repo, settings, f"每日检查完成，更新 {checked} 个项目")
        return {"checked_projects": checked, "publish": publish_result}

    @app.get("/api/publish/events")
    def list_publish_events():
        return [dict(row) for row in repo.list_publish_events()]

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard():
        return HTMLResponse(render_dashboard(repo))

    @app.post("/api/publish", dependencies=[Depends(require_write_auth)])
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
            url=extract_published_address(result.body) or settings.demo_publish_url,
            status_code=result.status_code,
            response_body=result.body,
            metadata={"ok": result.ok},
        )
        if not result.ok:
            raise HTTPException(status_code=result.status_code or 502, detail=result.body)
        return {"ok": True, "status_code": result.status_code}

    return app


def ingest_content(
    repo: Repository,
    settings: Settings,
    source: str | None,
    content: str,
    portfolio: bool,
    extractor: str,
    attachment_path: str | None = None,
):
    resolver = InstrumentResolver(repo.list_instruments())
    extraction = None
    if extractor in {"auto", "openai"} and settings.openai_api_key:
        extraction = OpenAISignalExtractor(settings.openai_api_key, settings.openai_model).extract(
            content,
            source_hint=source,
        )
    elif extractor == "openai":
        from fastapi import HTTPException

        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required for extractor=openai")
    source_name = resolve_source_name(source, content, extraction)
    if not source_name:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=422,
            detail={
                "code": "source_required",
                "message": "Provide source or include a first-line marker like 信息源：xxx.",
            },
        )
    ingest_body = remove_source_marker_lines(content) or content
    return SignalIngestor(
        repo,
        resolver,
        logic_supplementer=build_logic_supplementer(settings.openai_api_key, settings.openai_model),
    ).ingest(
        source_name=source_name,
        content=ingest_body,
        as_portfolio=portfolio,
        extraction=extraction,
        attachment_path=attachment_path,
    )


def result_response(repo: Repository, result, publish_result: dict) -> dict:
    return {
        "raw_input_id": result.raw_input_id,
        "project_ids": result.project_ids,
        "resolved_symbols": result.resolved_symbols,
        "projects": project_summaries(repo, result.project_ids),
        "logic_score": result.logic_score,
        "system_logic_added": result.system_logic_added,
        "publish": publish_result,
    }


def refresh_markets(value: str) -> list[Market]:
    if value == "all":
        return [Market.CN_A, Market.HK, Market.CN_FUT, Market.US, Market.US_FUT]
    try:
        return [Market(value)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown market: {value}") from exc


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
        url=extract_published_address(result.body) or settings.demo_publish_url,
        status_code=result.status_code,
        response_body=result.body,
        metadata={"ok": result.ok, "feature": feature},
    )
    return {"attempted": True, "ok": result.ok, "status_code": result.status_code}
