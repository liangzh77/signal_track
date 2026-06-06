from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, timedelta
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
from .input_files import UnsupportedInputFileError, read_input_file
from .logic_supplement import build_logic_supplementer
from .market_data import MarketDataService
from .market_smoke import market_data_smoke
from .models import Direction, Market, ProjectStatus
from .provider_diagnostics import market_data_coverage
from .project_actions import ProjectActionError, add_project_logic_block, close_tracking_project, update_tracking_project_weights
from .project_report import build_project_report, render_project_report_markdown
from .project_summary import project_summaries, project_summary
from .publisher import DemoPublisher, extract_published_address
from .providers.factory import build_market_data_provider
from .resolver import InstrumentResolver, SEED_INSTRUMENTS
from .signals import SignalIngestor
from .source_detection import remove_source_marker_lines, resolve_source_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="signal-track")
    parser.add_argument("--db", help="SQLite database path. Defaults to SIGNAL_TRACK_DB_PATH.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="Initialize the SQLite database.")
    subparsers.add_parser("migrate-db", help="Apply SQLite schema migrations.")

    backup_parser = subparsers.add_parser("backup-db", help="Create a safe SQLite backup.")
    backup_parser.add_argument("--out", help="Backup destination path.")

    verify_parser = subparsers.add_parser("verify-db", help="Verify SQLite integrity, foreign keys, schema version, and row counts.")
    verify_parser.add_argument("--allow-missing", action="store_true", help="Return a structured report instead of failing when the DB file is missing.")

    restore_parser = subparsers.add_parser("restore-db", help="Restore the configured SQLite DB from a verified backup.")
    restore_parser.add_argument("--from", dest="backup_path", required=True, help="Backup SQLite file to restore from.")
    restore_parser.add_argument("--force", action="store_true", help="Overwrite the destination DB if it already exists.")

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

    coverage_parser = subparsers.add_parser("market-coverage", help="Report configured market data coverage.")
    coverage_parser.add_argument(
        "--provider",
        choices=["none", "auto", "fixture", "tushare", "yfinance"],
        default="auto",
        help="Provider configuration to inspect without calling remote market APIs.",
    )

    smoke_parser = subparsers.add_parser("market-smoke", help="Fetch sample daily bars for configured markets.")
    smoke_parser.add_argument("--provider", choices=["auto", "fixture", "tushare", "yfinance"], default="auto")
    smoke_parser.add_argument(
        "--market",
        choices=["all", *[market.value for market in Market]],
        default="all",
    )
    smoke_parser.add_argument("--days", type=int, default=30)
    smoke_parser.add_argument("--sample-size", type=int, default=1)

    list_inputs_parser = subparsers.add_parser("list-inputs", help="List recent raw inputs and attachment paths.")
    list_inputs_parser.add_argument("--limit", type=int, default=50)

    list_projects_parser = subparsers.add_parser("list-projects", help="List tracking projects with performance snapshots.")
    list_projects_parser.add_argument("--source", help="Filter by exact source name.")
    list_projects_parser.add_argument("--status", choices=[status.value for status in ProjectStatus], help="Filter by project status.")
    list_projects_parser.add_argument("--direction", choices=[direction.value for direction in Direction], help="Filter by project direction.")
    list_projects_parser.add_argument("--no-performance", action="store_true", help="Omit return curves and leg performance.")
    list_projects_parser.add_argument("--limit", type=int, default=100)

    show_input_parser = subparsers.add_parser("show-input", help="Show a raw input with full content.")
    show_input_parser.add_argument("input_id", type=int)

    list_research_parser = subparsers.add_parser("list-research-items", help="List pending research verification items.")
    list_research_parser.add_argument("--project-id", type=int)
    list_research_parser.add_argument("--limit", type=int, default=50)

    list_exit_parser = subparsers.add_parser("list-exit-signals", help="List projects currently carrying exit signals.")
    list_exit_parser.add_argument("--limit", type=int, default=50)

    report_parser = subparsers.add_parser("export-project-report", help="Export a project research report.")
    report_parser.add_argument("project_id", type=int)
    report_parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    report_parser.add_argument("--out", help="Output path. Prints to stdout if omitted.")

    update_research_parser = subparsers.add_parser("update-research-item", help="Update a research verification item.")
    update_research_parser.add_argument("item_id", type=int)
    update_research_parser.add_argument(
        "--status",
        required=True,
        choices=["pending", "unverified", "verified", "contradicted", "ignored"],
    )
    update_research_parser.add_argument("--source-note")
    update_research_parser.add_argument("--metadata-json")
    update_research_parser.add_argument("--check", action="store_true", help="Run a daily check after updating.")
    update_research_parser.add_argument("--provider", choices=["none", "auto", "fixture", "tushare", "yfinance"], default="none")
    update_research_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after updating.")
    update_research_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
    update_research_parser.add_argument("--title", default="Signal Track 投资信号看板")

    close_project_parser = subparsers.add_parser("close-project", help="Manually close a tracking project.")
    close_project_parser.add_argument("project_id", type=int)
    close_project_parser.add_argument("--date", help="Close date, YYYY-MM-DD. Defaults to today.")
    close_project_parser.add_argument("--reason", help="Close reason recorded as close logic.")
    close_project_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after closing.")
    close_project_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
    close_project_parser.add_argument("--title", default="Signal Track 投资信号看板")

    weights_parser = subparsers.add_parser("update-project-weights", help="Update all leg weights for a portfolio project.")
    weights_parser.add_argument("project_id", type=int)
    weights_parser.add_argument("--weights-json", required=True, help='JSON map, e.g. {"300750.SZ":60,"600519.SH":40}.')
    weights_parser.add_argument("--note", help="Reason recorded as a weight_update logic block.")
    weights_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after updating weights.")
    weights_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
    weights_parser.add_argument("--title", default="Signal Track 投资信号看板")

    note_parser = subparsers.add_parser("add-project-note", help="Append manual observation logic to a project.")
    note_parser.add_argument("project_id", type=int)
    note_parser.add_argument("--text", help="Logic note. Reads stdin if omitted.")
    note_parser.add_argument(
        "--type",
        choices=["source_update", "system_logic", "manual_note"],
        default="source_update",
        help="Logic block type to append.",
    )
    note_parser.add_argument("--confidence", type=float, default=1.0)
    note_parser.add_argument("--evidence-json", help='Optional JSON array of evidence strings.')
    note_parser.add_argument("--check", action="store_true", help="Run a daily check after adding the note.")
    note_parser.add_argument("--provider", choices=["none", "auto", "fixture", "tushare", "yfinance"], default="none")
    note_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after adding the note.")
    note_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
    note_parser.add_argument("--title", default="Signal Track 投资信号看板")

    ingest_parser = subparsers.add_parser("ingest", help="Create tracking projects from raw source text.")
    ingest_parser.add_argument("--source")
    ingest_parser.add_argument("--text", help="Raw investment note. Reads stdin if omitted.")
    ingest_parser.add_argument("--file", help="Read raw investment note from a text/markdown file.")
    ingest_parser.add_argument("--portfolio", action="store_true", help="Treat all resolved instruments as one project.")
    ingest_parser.add_argument("--publish", action="store_true", help="Publish the dashboard after ingest.")
    ingest_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
    ingest_parser.add_argument(
        "--extractor",
        choices=["auto", "heuristic", "openai"],
        default="auto",
        help="Extraction engine for raw source text. auto uses OpenAI when OPENAI_API_KEY is configured.",
    )

    check_parser = subparsers.add_parser("check", help="Run daily checks for active projects.")
    check_parser.add_argument("--date", help="Check date, YYYY-MM-DD. Defaults to today.")
    check_parser.add_argument(
        "--provider",
        choices=["none", "auto", "fixture", "tushare", "yfinance"],
        help="Optional provider used to refresh prices before checking. Defaults to SIGNAL_TRACK_DAILY_PROVIDER.",
    )

    render_parser = subparsers.add_parser("render-dashboard", help="Render dashboard HTML.")
    render_parser.add_argument("--out", default="dist/dashboard.html")

    self_check_parser = subparsers.add_parser("self-check", help="Run a non-destructive end-to-end smoke check.")
    self_check_parser.add_argument("--provider", choices=["none", "fixture"], default="fixture")
    self_check_parser.add_argument("--out", help="Optional HTML output path.")

    publish_parser = subparsers.add_parser("publish-dashboard", help="Render and publish dashboard HTML.")
    publish_parser.add_argument("--title", default="Signal Track 投资信号看板")
    publish_parser.add_argument("--feature", default="Signal Track 自动发布")

    daily_parser = subparsers.add_parser("daily-run", help="Run the full daily check -> render -> optional publish flow.")
    daily_parser.add_argument("--date", help="Check date, YYYY-MM-DD. Defaults to today.")
    daily_parser.add_argument(
        "--provider",
        choices=["none", "auto", "fixture", "tushare", "yfinance"],
        help="Optional provider used to refresh prices before checking. Defaults to SIGNAL_TRACK_DAILY_PROVIDER.",
    )
    daily_parser.add_argument("--out", default="dist/dashboard.html")
    daily_parser.add_argument("--publish", action="store_true")
    daily_parser.add_argument("--no-publish", action="store_true", help="Disable auto publish for this update.")
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

    if args.command == "verify-db":
        report = db.verify(require_exists=not args.allow_missing)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] or (args.allow_missing and not report["exists"]) else 1

    if args.command == "restore-db":
        try:
            restored_path = db.restore(args.backup_path, force=args.force)
        except (FileExistsError, FileNotFoundError, ValueError) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "restored": str(restored_path), "backup": args.backup_path}, ensure_ascii=False))
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
                            "skipped": result.skipped,
                            "error": result.error,
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

    if args.command == "market-coverage":
        print(json.dumps(market_data_coverage(settings, args.provider), ensure_ascii=False, indent=2))
        return 0

    if args.command == "market-smoke":
        db.init()
        seed_if_empty(repo)
        provider = build_provider(args.provider, settings)
        result = market_data_smoke(
            repo,
            provider,
            markets=refresh_markets(args.market),
            days=args.days,
            sample_size=args.sample_size,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

    if args.command == "list-inputs":
        db.init()
        print(json.dumps({"inputs": input_summaries(repo, limit=args.limit)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-projects":
        db.init()
        rows = repo.list_project_rows()
        if args.source:
            rows = [row for row in rows if str(row["source_name"]) == args.source]
        if args.status:
            rows = [row for row in rows if str(row["status"]) == args.status]
        if args.direction:
            rows = [row for row in rows if str(row["direction"]) == args.direction]
        rows = rows[: args.limit]
        performances = (
            {int(row["id"]): project_performance(repo, int(row["id"])) for row in rows}
            if not args.no_performance
            else {}
        )
        projects = [
            project_summary(
                row,
                performance=performances.get(int(row["id"])),
                latest_check=next(iter(repo.list_daily_checks(project_id=int(row["id"]), limit=1)), None),
            )
            for row in rows
        ]
        print(json.dumps({"projects": projects}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "show-input":
        db.init()
        detail = input_detail(repo, args.input_id)
        if not detail:
            print(json.dumps({"ok": False, "code": "input_not_found"}, ensure_ascii=False))
            return 2
        print(json.dumps({"ok": True, "input": detail}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-research-items":
        db.init()
        items = [dict(row) for row in repo.list_research_items(project_id=args.project_id, limit=args.limit)]
        print(json.dumps({"items": items}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "list-exit-signals":
        db.init()
        print(json.dumps({"exit_signals": exit_signal_summaries(repo, limit=args.limit)}, ensure_ascii=False, indent=2))
        return 0

    if args.command == "export-project-report":
        db.init()
        report = build_project_report(repo, args.project_id)
        if not report:
            print(json.dumps({"ok": False, "code": "project_not_found"}, ensure_ascii=False))
            return 2
        content = (
            json.dumps(report, ensure_ascii=False, indent=2)
            if args.format == "json"
            else render_project_report_markdown(report)
        )
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            print(json.dumps({"ok": True, "path": str(out_path)}, ensure_ascii=False))
        else:
            print(content)
        return 0

    if args.command == "update-research-item":
        db.init()
        try:
            metadata = json.loads(args.metadata_json) if args.metadata_json else None
        except json.JSONDecodeError as exc:
            print(
                json.dumps(
                    {"ok": False, "code": "invalid_metadata_json", "message": str(exc)},
                    ensure_ascii=False,
                )
            )
            return 2
        item = repo.update_research_item(
            args.item_id,
            status=args.status,
            source_note=args.source_note,
            metadata=metadata,
        )
        if not item:
            print(json.dumps({"ok": False, "code": "research_item_not_found"}, ensure_ascii=False))
            return 2
        checked = None
        if args.check:
            provider = None if args.provider == "none" else build_provider(args.provider, settings)
            checked = DailyChecker(
                repo,
                provider,
                evaluator=build_daily_evaluator_from_settings(settings),
            ).run()
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title=args.title,
                feature=f"研究验证项更新：{args.status}",
                flow="research-item-update",
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "item": dict(item),
                    "checked_projects": checked,
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "close-project":
        db.init()
        project = close_tracking_project(
            repo,
            args.project_id,
            closed_date=parse_date(args.date).isoformat() if args.date else None,
            reason=args.reason,
        )
        if not project:
            print(json.dumps({"ok": False, "code": "project_not_found"}, ensure_ascii=False))
            return 2
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title=args.title,
                feature=f"Project {args.project_id} closed",
                flow="close-project",
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "project": project_summaries(repo, [args.project_id])[0],
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "update-project-weights":
        db.init()
        try:
            weights = json.loads(args.weights_json)
            if not isinstance(weights, dict):
                raise ValueError("weights-json must be a JSON object")
            weights = {str(key): float(value) for key, value in weights.items()}
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            print(json.dumps({"ok": False, "code": "invalid_weights_json", "message": str(exc)}, ensure_ascii=False))
            return 2
        try:
            project = update_tracking_project_weights(repo, args.project_id, weights, note=args.note)
        except ProjectActionError as exc:
            print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}, ensure_ascii=False))
            return 2
        if not project:
            print(json.dumps({"ok": False, "code": "project_not_found"}, ensure_ascii=False))
            return 2
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title=args.title,
                feature=f"Project {args.project_id} weights updated",
                flow="update-project-weights",
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "project": project_summaries(repo, [args.project_id])[0],
                    "legs": [dict(row) for row in repo.list_project_legs(args.project_id)],
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "add-project-note":
        db.init()
        note_text = args.text if args.text is not None else sys.stdin.read()
        evidence = None
        if args.evidence_json:
            try:
                parsed = json.loads(args.evidence_json)
                if not isinstance(parsed, list):
                    raise ValueError("evidence-json must be a JSON array")
                evidence = [str(item) for item in parsed]
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                print(json.dumps({"ok": False, "code": "invalid_evidence_json", "message": str(exc)}, ensure_ascii=False))
                return 2
        try:
            project = add_project_logic_block(
                repo,
                args.project_id,
                note_text,
                logic_type=args.type,
                confidence=args.confidence,
                evidence=evidence,
            )
        except ProjectActionError as exc:
            print(json.dumps({"ok": False, "code": exc.code, "message": exc.message}, ensure_ascii=False))
            return 2
        if not project:
            print(json.dumps({"ok": False, "code": "project_not_found"}, ensure_ascii=False))
            return 2
        checked = None
        if args.check:
            try:
                provider = build_market_data_provider(args.provider, settings)
            except ValueError as exc:
                print(json.dumps({"ok": False, "code": "provider_error", "message": str(exc)}, ensure_ascii=False))
                return 2
            checked = DailyChecker(
                repo,
                provider,
                evaluator=build_daily_evaluator_from_settings(settings),
            ).run()
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title=args.title,
                feature=f"Project {args.project_id} logic updated",
                flow="add-project-note",
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "project": project_summaries(repo, [args.project_id])[0],
                    "logic_blocks": [dict(row) for row in repo.list_logic_blocks(args.project_id)],
                    "checked_projects": checked,
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "ingest":
        db.init()
        seed_if_empty(repo)
        attachment_path = None
        if args.file:
            attachment = Path(args.file)
            try:
                content = read_input_file(attachment)
            except UnsupportedInputFileError as exc:
                print(json.dumps({"ok": False, "code": "unsupported_input_file", "message": str(exc)}, ensure_ascii=False))
                return 4
            attachment_path = str(attachment)
        else:
            content = args.text if args.text is not None else sys.stdin.read()
        resolver = InstrumentResolver(repo.list_instruments())
        extraction = None
        if args.extractor in {"auto", "openai"}:
            if not settings.openai_api_key:
                if args.extractor == "openai":
                    raise SystemExit("OPENAI_API_KEY is required for --extractor openai")
            else:
                try:
                    extraction = OpenAISignalExtractor(settings.openai_api_key, settings.openai_model).extract(
                        content,
                        source_hint=args.source,
                    )
                except Exception as exc:
                    if args.extractor == "openai":
                        raise SystemExit(f"OpenAI extractor failed: {exc}") from exc
                    extraction = None
        source_name = resolve_source_name(args.source, content, extraction)
        if not source_name:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "code": "source_required",
                        "message": "Provide --source or include a first-line marker like 信息源：xxx.",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 3
        ingest_body = remove_source_marker_lines(content) or content
        result = SignalIngestor(
            repo,
            resolver,
            logic_supplementer=build_logic_supplementer_from_settings(settings),
        ).ingest(
            source_name=source_name,
            content=ingest_body,
            as_portfolio=args.portfolio,
            extraction=extraction,
            attachment_path=attachment_path,
        )
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title="Signal Track 投资信号看板",
                feature="新增信息后自动发布",
                flow="ingest",
            )
        print(
            json.dumps(
                {
                    "raw_input_id": result.raw_input_id,
                    "project_ids": result.project_ids,
                    "resolved_symbols": result.resolved_symbols,
                    "projects": project_summaries(repo, result.project_ids),
                    "logic_score": result.logic_score,
                    "system_logic_added": result.system_logic_added,
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if publish_result is None or publish_result.ok else 1

    if args.command == "check":
        db.init()
        check_date = parse_date(args.date) if args.date else None
        provider_name = args.provider or settings.daily_provider
        provider = None if provider_name == "none" else build_provider(provider_name, settings)
        count = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_evaluator_from_settings(settings),
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

    if args.command == "self-check":
        result = run_self_check(settings, provider_name=args.provider, out=args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1

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
                {
                    "ok": result.ok,
                    "status_code": result.status_code,
                    "url": extract_published_address(result.body),
                    "publish_url": settings.demo_publish_url,
                    "body": result.body[:500],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if result.ok else 1

    if args.command == "daily-run":
        db.init()
        check_date = parse_date(args.date) if args.date else None
        provider_name = args.provider or settings.daily_provider
        provider = None if provider_name == "none" else build_provider(provider_name, settings)
        checked = DailyChecker(
            repo,
            provider,
            evaluator=build_daily_evaluator_from_settings(settings),
        ).run(check_date)
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        html = render_dashboard(repo)
        out_path.write_text(html, encoding="utf-8")
        publish_result = None
        if should_publish_update(settings, forced=args.publish, disabled=args.no_publish):
            publish_result = publish_dashboard(
                repo,
                settings,
                title=args.title,
                feature=f"每日检查完成，更新 {checked} 个项目",
                flow="daily-run",
            )
        print(
            json.dumps(
                {
                    "checked_projects": checked,
                    "html": str(out_path),
                    "published": publish_result.ok if publish_result else False,
                    "status_code": publish_result.status_code if publish_result else None,
                    "published_url": extract_published_address(publish_result.body) if publish_result else None,
                    "publish_url": settings.demo_publish_url if publish_result else None,
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
        os.environ["SIGNAL_TRACK_DB_PATH"] = str(db.path)
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


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def refresh_markets(value: str) -> list[Market]:
    if value == "all":
        return [Market.CN_A, Market.HK, Market.CN_FUT, Market.HK_FUT, Market.US, Market.US_FUT]
    return [Market(value)]


def should_publish_update(settings: Settings, forced: bool = False, disabled: bool = False) -> bool:
    if disabled:
        return False
    if forced:
        return True
    return bool(settings.auto_publish_on_update and settings.demo_publish_url and settings.demo_api_key)


def publish_dashboard(repo: Repository, settings: Settings, title: str, feature: str, flow: str):
    if not settings.demo_publish_url or not settings.demo_api_key:
        raise SystemExit("GO_SITES_DEMO_PUBLISH_URL and GO_SITES_DEMO_API_KEY are required")
    result = DemoPublisher(settings.demo_publish_url, settings.demo_api_key).publish(
        title=title,
        html=render_dashboard(repo),
        feature=feature,
    )
    repo.record_publish_event(
        title=title,
        url=extract_published_address(result.body) or settings.demo_publish_url,
        status_code=result.status_code,
        response_body=result.body,
        metadata={"ok": result.ok, "flow": flow},
    )
    return result


def run_self_check(settings: Settings, provider_name: str = "fixture", out: str | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "self-check.sqlite3")
        db.init()
        repo = Repository(db)
        for instrument in SEED_INSTRUMENTS:
            repo.upsert_instrument(instrument)
        resolver = InstrumentResolver(repo.list_instruments())
        scenario_results: dict[str, bool] = {}

        missing_source = resolve_source_name(None, "00700.HK long, observe ads and games.", None) is None
        scenario_results["requires_source"] = missing_source

        ingest_result = SignalIngestor(repo, resolver).ingest(
            source_name="self-check",
            content="00700.HK long, observe ads and games.",
        )
        scenario_results["single_project"] = bool(
            ingest_result.project_ids and ingest_result.resolved_symbols == ["00700.HK"]
        )

        low_logic = SignalIngestor(repo, resolver).ingest(
            source_name="self-check-low-logic",
            content="腾讯 做多，先跟踪。",
        )
        scenario_results["low_logic_supplement"] = bool(
            low_logic.project_ids
            and low_logic.system_logic_added
            and repo.list_research_items(project_id=low_logic.project_ids[0])
        )

        split = SignalIngestor(repo, resolver).ingest(
            source_name="self-check-split",
            content="00700.HK and NVDA long, watch ads and orders.",
        )
        scenario_results["multi_instrument_split"] = len(split.project_ids) == 2

        portfolio = SignalIngestor(repo, resolver).ingest(
            source_name="self-check-portfolio",
            content="portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
        )
        portfolio_legs = repo.list_project_legs(portfolio.project_ids[0]) if portfolio.project_ids else []
        scenario_results["portfolio_project"] = bool(
            len(portfolio.project_ids) == 1
            and len(portfolio_legs) == 2
            and not bool(repo.get_project_row(portfolio.project_ids[0])["weight_needs_review"])
        )

        provider = None if provider_name == "none" else build_provider(provider_name, settings)
        check_date = next_business_day(date.today()) if provider_name == "fixture" else date.today()
        checked = DailyChecker(repo, provider).run(check_date)
        html = render_dashboard(repo)
        html_path = None
        if out:
            output = Path(out)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(html, encoding="utf-8")
            html_path = str(output)
        projects = repo.list_project_rows()
        checks = repo.list_daily_checks()
        scenario_results["daily_checks"] = bool(checked == len(projects) and checks)
        scenario_results["dashboard"] = bool(
            "Signal Track" in html
            and "source-summary" in html
            and "leg-curves" in html
            and "研究验证项" in html
        )
        ok = bool(
            ingest_result.project_ids
            and ingest_result.resolved_symbols
            and all(scenario_results.values())
        )
        return {
            "ok": ok,
            "temporary_db": True,
            "project_ids": ingest_result.project_ids,
            "resolved_symbols": ingest_result.resolved_symbols,
            "checked_projects": checked,
            "daily_checks": len(checks),
            "project_count": len(projects),
            "scenario_results": scenario_results,
            "html": html_path,
        }


def next_business_day(value: date) -> date:
    current = value
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def default_backup_path(db_path: Path) -> Path:
    stamp = date.today().isoformat()
    return db_path.parent / f"{db_path.stem}-{stamp}.backup.sqlite3"


if __name__ == "__main__":
    raise SystemExit(main())
