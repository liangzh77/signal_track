from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from .checker import DailyChecker
from .config import Settings
from .daily_evaluator import build_daily_logic_evaluator
from .dashboard import render_dashboard
from .db import Database, Repository
from .extraction import OpenAISignalExtractor
from .instrument_master import InstrumentMasterService
from .logic_supplement import build_logic_supplementer
from .market_data import MarketDataService
from .models import Market
from .publisher import DemoPublisher, extract_published_address
from .providers.factory import build_market_data_provider
from .resolver import InstrumentResolver, SEED_INSTRUMENTS
from .signals import SignalIngestor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-track")
    parser.add_argument("--db", help="SQLite database path. Defaults to SIGNAL_TRACK_DB_PATH.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the SQLite database.")
    subparsers.add_parser("migrate-db", help="Apply SQLite schema migrations.")

    backup_parser = subparsers.add_parser("backup-db", help="Create a safe SQLite backup.")
    backup_parser.add_argument("--out", help="Backup destination path.")

    seed_parser = subparsers.add_parser("seed-instruments", help="Insert built-in seed instruments.")
    seed_parser.add_argument("--print", action="store_true", help="Print inserted instruments as JSON.")

    resolve_parser = subparsers.add_parser("resolve", help="Resolve a raw instrument name or code.")
    resolve_parser.add_argument("query")
    resolve_parser.add_argument("--market", choices=[market.value for market in Market])

    bars_parser = subparsers.add_parser("fetch-bars", help="Fetch and store daily bars.")
    bars_parser.add_argument("query", help="Instrument name, alias, or symbol.")
    bars_parser.add_argument("--provider", choices=["auto", "fixture", "tushare", "yfinance"], default="fixture")
    bars_parser.add_argument("--start")
    bars_parser.add_argument("--end")
    bars_parser.add_argument("--market", choices=[market.value for market in Market])

    refresh_parser = subparsers.add_parser("refresh-instruments", help="Refresh instrument master records.")
    refresh_parser.add_argument("--provider", choices=["auto", "fixture", "tushare"], default="tushare")
    refresh_parser.add_argument(
        "--market",
        choices=["all", *[market.value for market in Market]],
        default="all",
    )
    refresh_parser.add_argument("--sample", type=int, default=0, help="Print the first N refreshed symbols.")

    ingest_parser = subparsers.add_parser("ingest", help="Create tracking projects from raw source text.")
    ingest_parser.add_argument("--source", default="manual")
    ingest_parser.add_argument("--text", help="Raw investment note. Reads stdin if omitted.")
    ingest_parser.add_argument("--file", help="Read raw investment note from a text/markdown file.")
    ingest_parser.add_argument("--portfolio", action="store_true", help="Treat all resolved instruments as one project.")
    ingest_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after ingest.")
    ingest_parser.add_argument(
        "--extractor",
        choices=["heuristic", "openai"],
        default="heuristic",
        help="Extraction engine for raw source text.",
    )

    check_parser = subparsers.add_parser("check", help="Run daily checks for active projects.")
    check_parser.add_argument("--date", help="Check date, YYYY-MM-DD. Defaults to today.")
    check_parser.add_argument(
        "--provider",
        choices=["none", "auto", "fixture", "tushare", "yfinance"],
        default="none",
        help="Optional provider used to refresh prices before checking.",
    )

    render_parser = subparsers.add_parser("render-dashboard", help="Render dashboard HTML.")
    render_parser.add_argument("--out", default="dist/dashboard.html")

    publish_parser = subparsers.add_parser("publish-dashboard", help="Render and publish dashboard HTML.")
    publish_parser.add_argument("--title", default="Signal Track 投资信号看板")
    publish_parser.add_argument("--feature", default="Signal Track 自动发布")

    daily_parser = subparsers.add_parser("daily-run", help="Run the full daily check -> render -> optional publish flow.")
    daily_parser.add_argument("--date", help="Check date, YYYY-MM-DD. Defaults to today.")
    daily_parser.add_argument(
        "--provider",
        choices=["none", "auto", "fixture", "tushare", "yfinance"],
        default="none",
        help="Optional provider used to refresh prices before checking.",
    )
    daily_parser.add_argument("--out", default="dist/dashboard.html")
    daily_parser.add_argument("--publish", action="store_true")
    daily_parser.add_argument("--title", default="Signal Track 投资信号看板")

    serve_parser = subparsers.add_parser("serve", help="Run the FastAPI backend service.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    settings = Settings.from_env()
    db = Database(args.db or settings.db_path)
    repo = Repository(db)

    if args.command == "init-db":
        db.init()
        print(json.dumps({"ok": True, "db_path": str(db.path)}, ensure_ascii=False))
        return 0

    if args.command == "migrate-db":
        db.init()
        version = db.migrate()
        print(json.dumps({"ok": True, "db_path": str(db.path), "schema_version": version}, ensure_ascii=False))
        return 0

    if args.command == "backup-db":
        db.init()
        destination = args.out or default_backup_path(db.path)
        backup_path = db.backup(destination)
        print(json.dumps({"ok": True, "backup": str(backup_path)}, ensure_ascii=False))
        return 0

    if args.command == "seed-instruments":
        db.init()
        inserted = []
        for instrument in SEED_INSTRUMENTS:
            repo.upsert_instrument(instrument)
            inserted.append(instrument.symbol)
        if args.print:
            print(json.dumps({"inserted": inserted}, ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"inserted_count": len(inserted)}, ensure_ascii=False))
        return 0

    if args.command == "resolve":
        db.init()
        seed_if_empty(repo)
        resolver = InstrumentResolver(repo.list_instruments())
        market_hint = Market(args.market) if args.market else None
        resolution = resolver.resolve(args.query, market_hint)
        if not resolution:
            print(json.dumps({"resolved": False, "query": args.query}, ensure_ascii=False))
            return 2
        print(
            json.dumps(
                {
                    "resolved": True,
                    "symbol": resolution.instrument.symbol,
                    "name": resolution.instrument.name,
                    "market": resolution.instrument.market.value,
                    "asset_type": resolution.instrument.asset_type.value,
                    "confidence": resolution.confidence,
                    "reason": resolution.reason,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "fetch-bars":
        db.init()
        seed_if_empty(repo)
        resolver = InstrumentResolver(repo.list_instruments())
        market_hint = Market(args.market) if args.market else None
        resolution = resolver.resolve(args.query, market_hint)
        if not resolution:
            print(json.dumps({"resolved": False, "query": args.query}, ensure_ascii=False))
            return 2
        provider = build_provider(args.provider, settings)
        end = parse_date(args.end) if args.end else date.today()
        start = parse_date(args.start) if args.start else end - timedelta(days=730)
        service = MarketDataService(repo, provider)
        bars = service.fetch_and_store(resolution.instrument, start, end)
        print(
            json.dumps(
                {
                    "symbol": resolution.instrument.symbol,
                    "name": resolution.instrument.name,
                    "provider": provider.name,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "bar_count": len(bars),
                    "stored_bar_count": repo.count_price_bars(resolution.instrument.symbol),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "refresh-instruments":
        db.init()
        provider = build_provider(args.provider, settings)
        markets = refresh_markets(args.market)
        results = InstrumentMasterService(repo, provider).refresh_many(markets)
        print(
            json.dumps(
                {
                    "provider": provider.name,
                    "results": [
                        {
                            "market": result.market.value,
                            "count": result.count,
                            "sample": result.symbols[: args.sample] if args.sample else [],
                        }
                        for result in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "ingest":
        db.init()
        seed_if_empty(repo)
        attachment_path = None
        if args.file:
            attachment = Path(args.file)
            content = attachment.read_text(encoding="utf-8", errors="replace")
            attachment_path = str(attachment)
        else:
            content = args.text if args.text is not None else sys.stdin.read()
        resolver = InstrumentResolver(repo.list_instruments())
        extraction = None
        source_name = args.source
        if args.extractor == "openai":
            if not settings.openai_api_key:
                raise SystemExit("OPENAI_API_KEY is required for --extractor openai")
            extraction = OpenAISignalExtractor(settings.openai_api_key, settings.openai_model).extract(
                content,
                source_hint=args.source,
            )
            if args.source == "manual" and extraction.source_name:
                source_name = extraction.source_name
        result = SignalIngestor(
            repo,
            resolver,
            logic_supplementer=build_logic_supplementer(settings.openai_api_key, settings.openai_model),
        ).ingest(
            source_name=source_name,
            content=content,
            as_portfolio=args.portfolio,
            extraction=extraction,
            attachment_path=attachment_path,
        )
        publish_result = None
        if args.publish:
            if not settings.demo_publish_url or not settings.demo_api_key:
                raise SystemExit("GO_SITES_DEMO_PUBLISH_URL and GO_SITES_DEMO_API_KEY are required")
            html = render_dashboard(repo)
            publish_result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
                title="Signal Track 投资信号看板",
                html=html,
                feature="新增信息后自动发布",
            )
            repo.record_publish_event(
                title="Signal Track 投资信号看板",
                url=extract_published_address(publish_result.body) or settings.demo_publish_url,
                status_code=publish_result.status_code,
                response_body=publish_result.body,
                metadata={"ok": publish_result.ok, "flow": "ingest"},
            )
        print(
            json.dumps(
                {
                    "raw_input_id": result.raw_input_id,
                    "project_ids": result.project_ids,
                    "resolved_symbols": result.resolved_symbols,
                    "logic_score": result.logic_score,
                    "system_logic_added": result.system_logic_added,
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "check":
        db.init()
        check_date = parse_date(args.date) if args.date else None
        provider = None if args.provider == "none" else build_provider(args.provider, settings)
        count = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_logic_evaluator(settings.openai_api_key, settings.openai_model),
        ).run(check_date)
        print(json.dumps({"checked_projects": count}, ensure_ascii=False))
        return 0

    if args.command == "render-dashboard":
        db.init()
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_dashboard(repo), encoding="utf-8")
        print(json.dumps({"html": str(out_path)}, ensure_ascii=False))
        return 0

    if args.command == "publish-dashboard":
        db.init()
        if not settings.demo_publish_url or not settings.demo_api_key:
            raise SystemExit("GO_SITES_DEMO_PUBLISH_URL and GO_SITES_DEMO_API_KEY are required")
        html = render_dashboard(repo)
        result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
            title=args.title,
            html=html,
            feature=args.feature,
        )
        repo.record_publish_event(
            title=args.title,
            url=extract_published_address(result.body) or settings.demo_publish_url,
            status_code=result.status_code,
            response_body=result.body,
            metadata={"ok": result.ok},
        )
        print(
            json.dumps(
                {"ok": result.ok, "status_code": result.status_code, "body": result.body[:500]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.ok else 1

    if args.command == "daily-run":
        db.init()
        check_date = parse_date(args.date) if args.date else None
        provider = None if args.provider == "none" else build_provider(args.provider, settings)
        checked = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_logic_evaluator(settings.openai_api_key, settings.openai_model),
        ).run(check_date)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html = render_dashboard(repo)
        out_path.write_text(html, encoding="utf-8")
        publish_result = None
        if args.publish:
            if not settings.demo_publish_url or not settings.demo_api_key:
                raise SystemExit("GO_SITES_DEMO_PUBLISH_URL and GO_SITES_DEMO_API_KEY are required")
            publish_result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
                title=args.title,
                html=html,
                feature=f"每日检查完成，更新 {checked} 个项目",
            )
            repo.record_publish_event(
                title=args.title,
                url=extract_published_address(publish_result.body) or settings.demo_publish_url,
                status_code=publish_result.status_code,
                response_body=publish_result.body,
                metadata={"ok": publish_result.ok, "flow": "daily-run"},
            )
        print(
            json.dumps(
                {
                    "checked_projects": checked,
                    "html": str(out_path),
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "serve":
        try:
            import uvicorn
        except ImportError as exc:
            raise SystemExit("Install web extras first: pip install -e .[web]") from exc
        uvicorn.run("signal_track.web_app:create_app", factory=True, host=args.host, port=args.port)
        return 0

    parser.error(f"Unknown command {args.command}")
    return 2


def seed_if_empty(repo: Repository) -> None:
    if repo.list_instruments():
        return
    for instrument in SEED_INSTRUMENTS:
        repo.upsert_instrument(instrument)


def build_provider(name: str, settings: Settings):
    try:
        provider = build_market_data_provider(name, settings)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if provider is None:
        raise SystemExit("A concrete market data provider is required here")
    return provider


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def refresh_markets(value: str) -> list[Market]:
    if value == "all":
        return [Market.CN_A, Market.HK, Market.CN_FUT, Market.US, Market.US_FUT]
    return [Market(value)]


def default_backup_path(db_path: Path) -> Path:
    stamp = date.today().isoformat()
    return db_path.parent / f"{db_path.stem}-{stamp}.backup.sqlite3"


if __name__ == "__main__":
    raise SystemExit(main())
