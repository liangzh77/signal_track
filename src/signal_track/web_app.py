import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .analytics import project_performance
from .checker import DailyChecker
from .config import Settings
from .daily_evaluator import build_daily_logic_evaluator
from .dashboard import render_dashboard
from .db import Database, Repository
from .extraction import OpenAISignalExtractor
from .exit_signals import exit_signal_summaries
from .instrument_master import InstrumentMasterService
from .input_summary import input_detail, input_summaries
from .logic_supplement import build_logic_supplementer
from .market_smoke import market_data_smoke
from .models import Direction, Market
from .provider_diagnostics import market_data_coverage
from .project_actions import ProjectActionError, close_tracking_project, update_tracking_project_weights
from .project_report import build_project_report, render_project_report_markdown
from .project_summary import project_summaries, project_summary
from .publisher import DemoPublisher, extract_published_address, publish_payload
from .providers.factory import build_market_data_provider
from .resolver import InstrumentResolver, SEED_INSTRUMENTS
from .scheduler import build_scheduler, scheduler_job_summaries
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
        from fastapi.responses import HTMLResponse, PlainTextResponse
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
        provider: str | None = None
        date: str | None = None

    class RefreshInstrumentsPayload(BaseModel):
        provider: str = "tushare"
        market: str = "all"

    class ResearchItemUpdatePayload(BaseModel):
        status: str
        source_note: str | None = None
        metadata: dict | None = None
        run_check: bool = False
        provider: str = "none"

    class CloseProjectPayload(BaseModel):
        closed_date: str | None = None
        reason: str | None = None

    class ProjectWeightsPayload(BaseModel):
        weights: dict[str, float]
        note: str | None = None

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
            evaluator=build_daily_evaluator_from_settings(settings),
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
        scheduler_jobs = scheduler_job_summaries(scheduled_jobs.scheduler) if scheduled_jobs else []
        return health_payload(repo, scheduler_enabled=settings.enable_scheduler, scheduler_jobs=scheduler_jobs)

    @app.get("/api/market-data/coverage")
    def market_coverage(provider: str = "auto"):
        try:
            return market_data_coverage(settings, provider)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/market-data/smoke")
    def market_smoke(provider: str = "auto", market: str = "all", days: int = 30, sample_size: int = 1):
        try:
            market_provider = build_market_data_provider(provider, settings)
        except ValueError as exc:
            raise provider_http_exception(exc) from exc
        if market_provider is None:
            raise HTTPException(status_code=400, detail="A concrete provider is required")
        return market_data_smoke(
            repo,
            market_provider,
            markets=refresh_markets(market),
            days=days,
            sample_size=sample_size,
        )

    @app.get("/api/inputs")
    def list_inputs(limit: int = 100):
        return input_summaries(repo, limit=limit)

    @app.get("/api/inputs/{input_id}")
    def get_input(input_id: int):
        detail = input_detail(repo, input_id)
        if not detail:
            raise HTTPException(status_code=404, detail="Input not found")
        return detail

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
        content_bytes = await file.read()
        attachment_path = save_unique_attachment(attachments_dir, file.filename, content_bytes)
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
    def list_projects(source: str | None = None, status: str | None = None, direction: Direction | None = None):
        rows = repo.list_project_rows()
        if source:
            rows = [row for row in rows if str(row["source_name"]) == source]
        if status:
            rows = [row for row in rows if str(row["status"]) == status]
        if direction:
            rows = [row for row in rows if str(row["direction"]) == direction.value]
        performances = {int(row["id"]): project_performance(repo, int(row["id"])) for row in rows}
        return [
            project_summary(
                row,
                performance=performances[int(row["id"])],
                latest_check=next(iter(repo.list_daily_checks(project_id=int(row["id"]), limit=1)), None),
            )
            for row in rows
        ]

    @app.get("/api/exit-signals")
    def list_exit_signals(limit: int = 100):
        return exit_signal_summaries(repo, limit=limit)

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
            raise provider_http_exception(exc) from exc
        if provider is None:
            raise HTTPException(status_code=400, detail="A concrete provider is required")
        markets = refresh_markets(payload.market)
        results = InstrumentMasterService(repo, provider).refresh_many(markets)
        return {
            "provider": provider.name,
            "results": [
                {
                    "market": result.market.value,
                    "count": result.count,
                    "skipped": result.skipped,
                    "error": result.error,
                }
                for result in results
            ],
        }

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: int):
        project = repo.get_project_row(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        performance = project_performance(repo, project_id)
        summary_row = repo.list_project_rows_by_ids([project_id])[0]
        latest_check = next(iter(repo.list_daily_checks(project_id=project_id, limit=1)), None)
        return {
            "project": dict(project),
            "summary": project_summary(summary_row, performance=performance, latest_check=latest_check),
            "legs": [dict(row) for row in repo.list_project_legs(project_id)],
            "logic_blocks": [dict(row) for row in repo.list_logic_blocks(project_id)],
            "research_items": [dict(row) for row in repo.list_research_items(project_id=project_id)],
            "daily_checks": [dict(row) for row in repo.list_daily_checks(project_id=project_id)],
            "performance": {
                "return_pct": performance.return_pct,
                "latest_date": performance.latest_date,
                "points": performance.points,
                "point_count": len(performance.points),
                "window_start": performance.window_start,
                "window_end": performance.window_end,
                "missing_price_symbols": performance.missing_price_symbols,
                "legs": [leg.__dict__ for leg in performance.legs],
            },
        }

    @app.get("/api/projects/{project_id}/report")
    def get_project_report(project_id: int, format: str = "markdown"):
        report = build_project_report(repo, project_id)
        if not report:
            raise HTTPException(status_code=404, detail="Project not found")
        if format == "json":
            return report
        if format == "markdown":
            return PlainTextResponse(render_project_report_markdown(report), media_type="text/markdown; charset=utf-8")
        raise HTTPException(status_code=400, detail="format must be markdown or json")

    @app.post("/api/projects/{project_id}/close", dependencies=[Depends(require_write_auth)])
    def close_project(project_id: int, payload: CloseProjectPayload = Body(default=CloseProjectPayload())):
        closed_date = payload.closed_date
        if closed_date:
            try:
                closed_date = date.fromisoformat(closed_date).isoformat()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid closed_date") from exc
        project = close_tracking_project(
            repo,
            project_id,
            closed_date=closed_date,
            reason=payload.reason,
        )
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        publish_result = maybe_publish(repo, settings, f"Project {project_id} closed")
        return {"project": project_summaries(repo, [project_id])[0], "publish": publish_result}

    @app.patch("/api/projects/{project_id}/weights", dependencies=[Depends(require_write_auth)])
    def update_project_weights(project_id: int, payload: ProjectWeightsPayload):
        try:
            project = update_tracking_project_weights(
                repo,
                project_id,
                payload.weights,
                note=payload.note,
            )
        except ProjectActionError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": exc.message}) from exc
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        publish_result = maybe_publish(repo, settings, f"Project {project_id} weights updated")
        return {
            "project": project_summaries(repo, [project_id])[0],
            "legs": [dict(row) for row in repo.list_project_legs(project_id)],
            "publish": publish_result,
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
                raise provider_http_exception(exc) from exc
            checked = DailyChecker(
                repo,
                provider,
                evaluator=build_daily_evaluator_from_settings(settings),
            ).run()
        publish_result = maybe_publish(repo, settings, f"研究验证项更新：{payload.status}")
        return {"item": dict(item), "checked_projects": checked, "publish": publish_result}

    @app.post("/api/checks/run", dependencies=[Depends(require_write_auth)])
    def run_checks(payload: CheckPayload = Body(default=CheckPayload())):
        provider_name = payload.provider or settings.daily_provider
        try:
            provider = build_market_data_provider(provider_name, settings)
        except ValueError as exc:
            raise provider_http_exception(exc) from exc
        check_date = None
        if payload.date:
            try:
                check_date = date.fromisoformat(payload.date)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid check date") from exc
        checked = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_evaluator_from_settings(settings),
        ).run(check_date)
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
        published_url = extract_published_address(result.body)
        repo.record_publish_event(
            title="Signal Track 投资信号看板",
            url=published_url or settings.demo_publish_url,
            status_code=result.status_code,
            response_body=result.body,
            metadata={"ok": result.ok},
        )
        if not result.ok:
            raise HTTPException(status_code=result.status_code or 502, detail=result.body)
        return {"ok": True, "status_code": result.status_code, "url": published_url, "publish_url": settings.demo_publish_url}

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
    extractor = normalize_extractor(extractor)
    if extractor in {"auto", "openai"}:
        from fastapi import HTTPException

        if not settings.openai_api_key:
            if extractor == "openai":
                raise HTTPException(status_code=503, detail="OPENAI_API_KEY is required for extractor=openai")
        else:
            try:
                extraction = OpenAISignalExtractor(settings.openai_api_key, settings.openai_model).extract(
                    content,
                    source_hint=source,
                )
            except Exception as exc:
                if extractor == "openai":
                    raise HTTPException(status_code=503, detail=f"OpenAI extractor failed: {exc}") from exc
                extraction = None
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
        logic_supplementer=build_logic_supplementer_from_settings(settings),
    ).ingest(
        source_name=source_name,
        content=ingest_body,
        as_portfolio=portfolio,
        extraction=extraction,
        attachment_path=attachment_path,
    )


def normalize_extractor(value: str) -> str:
    extractor = (value or "auto").strip().lower()
    if extractor in {"auto", "openai", "heuristic"}:
        return extractor
    from fastapi import HTTPException

    raise HTTPException(status_code=400, detail=f"Unknown extractor: {value}")


def provider_http_exception(exc: ValueError):
    from fastapi import HTTPException

    message = str(exc)
    status_code = 400 if message.startswith("Unknown market data provider") else 503
    return HTTPException(status_code=status_code, detail=message)


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
        return [Market.CN_A, Market.HK, Market.CN_FUT, Market.HK_FUT, Market.US, Market.US_FUT]
    try:
        return [Market(value)]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Unknown market: {value}") from exc


def maybe_publish(repo: Repository, settings: Settings, feature: str) -> dict:
    if not settings.auto_publish_on_update:
        return {
            "attempted": False,
            "ok": False,
            "url": None,
            "publish_url": settings.demo_publish_url,
            "reason": "auto publish disabled",
        }
    if not settings.demo_publish_url or not settings.demo_api_key:
        return {
            "attempted": False,
            "ok": False,
            "url": None,
            "publish_url": settings.demo_publish_url,
            "reason": "publish credentials not configured",
        }
    result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
        title="Signal Track 投资信号看板",
        html=render_dashboard(repo),
        feature=feature,
    )
    payload = publish_payload(result, settings.demo_publish_url)
    repo.record_publish_event(
        title="Signal Track 投资信号看板",
        url=payload["url"] or settings.demo_publish_url,
        status_code=result.status_code,
        response_body=result.body,
        metadata={"ok": result.ok, "feature": feature},
    )
    return payload


def health_payload(
    repo: Repository,
    scheduler_enabled: bool = False,
    scheduler_jobs: list[dict[str, str | None]] | None = None,
) -> dict:
    try:
        projects = repo.list_project_rows()
        checks = repo.list_daily_checks(limit=1)
        publish_events = repo.list_publish_events(limit=1)
    except Exception as exc:
        return {
            "ok": False,
            "database": {"ok": False, "error": str(exc)},
            "scheduler_enabled": scheduler_enabled,
            "scheduler_jobs": scheduler_jobs or [],
        }

    active = sum(1 for row in projects if row["status"] in {"active", "needs_review"})
    exit_signals = sum(1 for row in projects if row["status"] == "exit_signal")
    review = sum(1 for row in projects if bool(row["needs_review"]) or bool(row["weight_needs_review"]))
    latest_check = checks[0] if checks else None
    latest_publish = publish_events[0] if publish_events else None
    publish_metadata = parse_json_dict(latest_publish["metadata"]) if latest_publish else {}
    degraded_reasons = []
    if latest_publish and publish_metadata.get("ok") is False:
        degraded_reasons.append("latest_publish_failed")
    return {
        "ok": not degraded_reasons,
        "database": {"ok": True},
        "scheduler_enabled": scheduler_enabled,
        "scheduler_jobs": scheduler_jobs or [],
        "degraded_reasons": degraded_reasons,
        "projects": {
            "total": len(projects),
            "active_or_review": active,
            "exit_signal": exit_signals,
            "needs_review": review,
        },
        "latest_check": (
            {
                "project_id": int(latest_check["project_id"]),
                "project_title": latest_check["title"],
                "check_date": latest_check["check_date"],
                "conclusion": latest_check["conclusion"],
            }
            if latest_check
            else None
        ),
        "latest_publish": (
            {
                "published_at": latest_publish["published_at"],
                "status_code": latest_publish["status_code"],
                "ok": publish_metadata.get("ok"),
                "url": latest_publish["url"],
            }
            if latest_publish
            else None
        ),
    }


def parse_json_dict(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_daily_evaluator_from_settings(settings: Settings):
    return build_daily_logic_evaluator(
        settings.openai_api_key,
        settings.openai_model,
        web_research=settings.openai_web_research,
        web_search_context_size=settings.openai_web_search_context_size,
    )


def build_logic_supplementer_from_settings(settings: Settings):
    return build_logic_supplementer(
        settings.openai_api_key,
        settings.openai_model,
        web_research=settings.openai_web_research,
        web_search_context_size=settings.openai_web_search_context_size,
    )


def save_unique_attachment(directory: Path, filename: str | None, content: bytes) -> Path:
    safe_name = Path(filename or "input.txt").name
    if safe_name in {"", ".", ".."}:
        safe_name = "input.txt"
    base = Path(safe_name)
    stem = base.stem or "input"
    suffix = base.suffix
    for index in range(1000):
        candidate_name = safe_name if index == 0 else f"{stem}-{index}{suffix}"
        candidate = directory / candidate_name
        try:
            with candidate.open("xb") as handle:
                handle.write(content)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not allocate unique attachment path for {safe_name}")
