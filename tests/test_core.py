from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
from datetime import date, timedelta
from pathlib import Path

from signal_track.config import Settings
from signal_track.db import Database, Repository
from signal_track.checker import DailyChecker
from signal_track.cli import refresh_markets as cli_refresh_markets
from signal_track.cli import main as cli_main
from signal_track.cli import run_self_check
from signal_track.dashboard import render_dashboard
from signal_track.analytics import project_performance
from signal_track.daily_evaluator import DailyEvaluation, DailyLogicEvaluator
from signal_track.exit_signals import exit_signal_summaries
from signal_track.extraction import ExtractedInput, ExtractedSignal
from signal_track.instrument_master import InstrumentMasterService
from signal_track.logic_supplement import LogicSupplement, LogicSupplementer
from signal_track.market_data import MarketDataService
from signal_track.models import DailyBar, Instrument, Market
from signal_track.provider_diagnostics import market_data_coverage
from signal_track.publisher import extract_published_address
from signal_track.publisher import PublishResult
from signal_track.providers.auto import AutoMarketDataProvider
from signal_track.providers.base import MarketDataProvider
from signal_track.providers.factory import build_auto_provider
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.providers.yfinance_provider import get_price_field
from signal_track.project_actions import ProjectActionError, update_tracking_project_weights
from signal_track.resolver import InstrumentResolver, SEED_INSTRUMENTS
from signal_track.signals import SignalIngestor
from signal_track.source_detection import resolve_source_name

try:
    from fastapi.testclient import TestClient
    from signal_track.web_app import create_app
except Exception:
    TestClient = None
    create_app = None

from signal_track.scheduler import execute_daily_check


def next_fixture_trading_day(current: date) -> date:
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


class RecordingMarketDataProvider(MarketDataProvider):
    def __init__(self, name: str, instruments: list[Instrument] | None = None):
        self.name = name
        self.calls: list[str] = []
        self.instruments = instruments

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        del start_date, end_date, adjustment
        self.calls.append(instrument.symbol)
        return [
            DailyBar(
                symbol=instrument.symbol,
                provider_symbol=instrument.provider_symbol,
                date=date(2026, 6, 5),
                open=100,
                high=101,
                low=99,
                close=100,
                provider=self.name,
            )
        ]

    def list_instruments(self, market: Market) -> list[Instrument]:
        if self.instruments is None:
            raise NotImplementedError
        return [instrument for instrument in self.instruments if instrument.market == market]


class PartiallyFailingMarketDataProvider(RecordingMarketDataProvider):
    def __init__(self, failing_symbols: set[str]):
        super().__init__("partial")
        self.failing_symbols = failing_symbols

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        if instrument.symbol in self.failing_symbols:
            self.calls.append(instrument.symbol)
            raise RuntimeError("provider unavailable")
        del start_date, adjustment
        self.calls.append(instrument.symbol)
        return [
            DailyBar(
                symbol=instrument.symbol,
                provider_symbol=instrument.provider_symbol,
                date=end_date,
                open=100,
                high=101,
                low=99,
                close=100,
                provider=self.name,
            )
        ]


class FakeSeries:
    def __init__(self, value: float):
        self.iloc = [value]


class FakeLogicSupplementer(LogicSupplementer):
    def supplement(self, *, name, direction, source_logic, instruments):
        del name, direction, source_logic, instruments
        return LogicSupplement(
            thesis="AI补充：围绕广告、游戏与估值修复建立跟踪假设。",
            tracking_metrics=["广告收入环比改善", "游戏流水恢复", "股价跌破 20 日线"],
            exit_conditions=["跌破 20 日线", "核心业务恢复低于预期"],
            verification_notes=["财务和行业数据需要外部来源交叉验证"],
            confidence=0.7,
        )


class BrokenLogicSupplementer(LogicSupplementer):
    def supplement(self, *, name, direction, source_logic, instruments):
        del name, direction, source_logic, instruments
        raise RuntimeError("boom")


class FakeDemoPublisher:
    def __init__(self, publish_url: str, api_key: str):
        self.publish_url = publish_url
        self.api_key = api_key

    def publish(self, title: str, html: str, feature: str = "", disabled: bool = False) -> PublishResult:
        del title, html, feature, disabled
        return PublishResult(True, 200, '{"address":"https://example.com/demo/signal"}')


class FakeOpenAIExtractor:
    calls: list[dict[str, str | None]] = []

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def extract(self, content: str, source_hint: str | None = None) -> ExtractedInput:
        self.calls.append({"api_key": self.api_key, "model": self.model, "source_hint": source_hint})
        return ExtractedInput(
            signals=[
                ExtractedSignal(
                    instruments=["NVDA"],
                    direction="short",
                    source_logic=f"structured: {content}",
                    observation_logic="watch orders and margin.",
                    logic_score=8,
                    action="open",
                )
            ],
        )


class FakeDailyEvaluator(DailyLogicEvaluator):
    def __init__(self):
        self.research_item_count = 0

    def evaluate(self, *, project, logic_blocks, research_items, performance, previous_checks, check_date):
        del project, logic_blocks, performance, previous_checks, check_date
        self.research_item_count = len(research_items)
        return DailyEvaluation(
            conclusion="exit_signal",
            summary="核心跟踪假设被证伪，建议平仓。",
            triggered_rules=["逻辑评估：核心假设被证伪"],
            confidence=0.8,
        )


class SignalTrackCoreTests(unittest.TestCase):
    def test_settings_default_daily_provider_is_auto(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_ENABLE_SCHEDULER": "false",
                "SIGNAL_TRACK_DAILY_PROVIDER": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.daily_provider, "auto")

    def test_resolves_seed_instruments_across_markets(self) -> None:
        resolver = InstrumentResolver()

        cases = [
            ("宁德时代", Market.CN_A, "300750.SZ"),
            ("00700", Market.HK, "00700.HK"),
            ("铜主连", Market.CN_FUT, "CU.SHF"),
            ("恒指期货", Market.HK_FUT, "HSI"),
            ("NVDA.US", Market.US, "NVDA"),
            ("纳指期货", Market.US_FUT, "NQ"),
        ]

        for query, market, expected in cases:
            with self.subTest(query=query):
                resolution = resolver.resolve(query, market)
                self.assertIsNotNone(resolution)
                self.assertEqual(resolution.instrument.symbol, expected)
                self.assertGreaterEqual(resolution.confidence, 0.6)

    def test_resolver_synthesizes_explicit_unknown_symbols(self) -> None:
        resolver = InstrumentResolver([SEED_INSTRUMENTS[0]])

        cases = [
            ("002594", None, "002594.SZ", Market.CN_A),
            ("9868", Market.HK, "09868.HK", Market.HK),
            ("TSLA.US", None, "TSLA", Market.US),
            ("TSLA", None, "TSLA", Market.US),
            ("MHI=F", Market.HK_FUT, "MHI", Market.HK_FUT),
            ("ES=F", None, "ES", Market.US_FUT),
            ("CU2601.SHF", None, "CU2601.SHF", Market.CN_FUT),
        ]

        for query, market_hint, symbol, market in cases:
            with self.subTest(query=query):
                resolution = resolver.resolve(query, market_hint)
                self.assertIsNotNone(resolution)
                self.assertEqual(resolution.instrument.symbol, symbol)
                self.assertEqual(resolution.instrument.market, market)
                self.assertTrue(resolution.instrument.metadata["synthetic"])

        self.assertIsNone(resolver.resolve("60"))
        self.assertIsNone(resolver.resolve("long"))
        self.assertIsNone(resolver.resolve("HK"))
        self.assertIsNone(resolver.resolve("if"))
        self.assertIsNone(resolver.resolve("pipeline"))
        self.assertIsNone(resolver.resolve("IF"))
        self.assertEqual(InstrumentResolver().resolve("IF").instrument.symbol, "IF.CFX")

    def test_database_initialization_and_fixture_bar_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            instrument = SEED_INSTRUMENTS[0]
            service = MarketDataService(repo, FixtureMarketDataProvider())

            bars = service.fetch_and_store(
                instrument,
                date(2026, 1, 1),
                date(2026, 1, 15),
            )

            self.assertGreater(len(bars), 0)
            self.assertEqual(repo.count_price_bars(instrument.symbol), len(bars))
            stored = repo.get_instrument(instrument.symbol)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.name, "宁德时代")

    def test_database_migration_adds_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.sqlite3"
            db = Database(db_path)
            with db.session() as conn:
                conn.execute(
                    """
                    CREATE TABLE tracking_projects (
                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                      title TEXT NOT NULL,
                      source_id INTEGER NOT NULL,
                      raw_input_id INTEGER,
                      status TEXT NOT NULL,
                      direction TEXT NOT NULL
                    )
                    """
                )

            version = db.migrate()
            self.assertEqual(version, 2)
            with db.session() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracking_projects)")}
                research_columns = {row["name"] for row in conn.execute("PRAGMA table_info(research_items)")}
            self.assertIn("logic_score", columns)
            self.assertIn("weight_needs_review", columns)
            self.assertIn("item_type", research_columns)
            self.assertIn("status", research_columns)

    def test_database_backup_creates_readable_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            repo.upsert_instrument(SEED_INSTRUMENTS[0])

            backup_path = db.backup(Path(tmp) / "backup.sqlite3")

            backup_repo = Repository(Database(backup_path))
            self.assertIsNotNone(backup_repo.get_instrument("300750.SZ"))

    def test_fixture_instrument_master_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            result = InstrumentMasterService(repo, FixtureMarketDataProvider()).refresh(Market.CN_A)

            self.assertGreaterEqual(result.count, 2)
            self.assertIsNotNone(repo.get_instrument("300750.SZ"))
            self.assertIsNotNone(repo.get_instrument("600519.SH"))

    def test_instrument_master_refresh_many_marks_unsupported_markets_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            results = InstrumentMasterService(repo, RecordingMarketDataProvider("limited")).refresh_many(
                [Market.HK_FUT]
            )

            self.assertEqual(results[0].market, Market.HK_FUT)
            self.assertTrue(results[0].skipped)
            self.assertIsNotNone(results[0].error)

    def test_refresh_all_markets_includes_hk_and_us_futures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            markets = cli_refresh_markets("all")

            self.assertIn(Market.HK_FUT, markets)
            self.assertIn(Market.US_FUT, markets)
            results = InstrumentMasterService(repo, FixtureMarketDataProvider()).refresh_many(markets)

            self.assertTrue(any(result.market == Market.HK_FUT and result.count >= 2 for result in results))
            self.assertTrue(any(result.market == Market.US_FUT and result.count >= 2 for result in results))
            self.assertIsNotNone(repo.get_instrument("HSI"))
            self.assertIsNotNone(repo.get_instrument("NQ"))

    def test_auto_market_provider_routes_by_market_and_falls_back_to_seed_master(self) -> None:
        cn_provider = RecordingMarketDataProvider("cn")
        hk_future_provider = RecordingMarketDataProvider("hk-fut")
        us_provider = RecordingMarketDataProvider("us")
        provider = AutoMarketDataProvider.from_market_map(
            {
                Market.CN_A: cn_provider,
                Market.HK_FUT: hk_future_provider,
                Market.US_FUT: us_provider,
            }
        )

        provider.get_daily_bars(SEED_INSTRUMENTS[0], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "HSI"), date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(SEED_INSTRUMENTS[-1], date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(cn_provider.calls, ["300750.SZ"])
        self.assertEqual(hk_future_provider.calls, ["HSI"])
        self.assertEqual(us_provider.calls, ["NQ"])
        self.assertEqual([instrument.symbol for instrument in provider.list_instruments(Market.HK_FUT)], ["HSI", "HHI"])
        self.assertEqual([instrument.symbol for instrument in provider.list_instruments(Market.US_FUT)], ["ES", "NQ"])

    def test_auto_provider_prefers_tushare_and_uses_yfinance_for_futures_fallbacks(self) -> None:
        tushare_provider = RecordingMarketDataProvider("tushare")
        yfinance_provider = RecordingMarketDataProvider("yfinance")
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            enable_scheduler=False,
            daily_provider="auto",
            openai_api_key=None,
            openai_model="model",
            signal_track_api_key=None,
        )

        with patch("signal_track.providers.factory.TushareMarketDataProvider", return_value=tushare_provider):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "HSI"), date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(SEED_INSTRUMENTS[-1], date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(tushare_provider.calls, ["00700.HK"])
        self.assertEqual(yfinance_provider.calls, ["HSI", "NQ"])

    def test_yfinance_price_field_handles_multiindex_shapes(self) -> None:
        normal_row = {"Close": 101.5}
        field_first_row = {("Close", "AAPL"): 102.5}
        ticker_first_row = {("AAPL", "Close"): 103.5}
        series_row = {"Close": FakeSeries(104.5)}

        self.assertEqual(get_price_field(normal_row, "AAPL", "Close"), 101.5)
        self.assertEqual(get_price_field(field_first_row, "AAPL", "Close"), 102.5)
        self.assertEqual(get_price_field(ticker_first_row, "AAPL", "Close"), 103.5)
        self.assertEqual(get_price_field(series_row, "AAPL", "Close"), 104.5)
        self.assertIsNone(get_price_field({}, "AAPL", "Close"))

    def test_market_coverage_reports_auto_routes_without_remote_calls(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            enable_scheduler=False,
            daily_provider="auto",
            openai_api_key=None,
            openai_model="model",
            signal_track_api_key=None,
        )

        with patch("signal_track.provider_diagnostics.find_spec", return_value=object()):
            coverage = market_data_coverage(settings, "auto")

        by_market = {row["market"]: row for row in coverage["markets"]}
        self.assertEqual(by_market["CN_A"]["price_provider"], "tushare")
        self.assertEqual(by_market["HK"]["price_provider"], "tushare")
        self.assertEqual(by_market["CN_FUT"]["instrument_master_provider"], "tushare")
        self.assertEqual(by_market["HK_FUT"]["price_provider"], "yfinance")
        self.assertEqual(by_market["HK_FUT"]["instrument_master_provider"], "seed_fallback")
        self.assertFalse(by_market["HK_FUT"]["real_instrument_master"])
        self.assertEqual(by_market["US_FUT"]["price_provider"], "yfinance")
        self.assertEqual(by_market["US_FUT"]["instrument_master_provider"], "seed_fallback")
        self.assertFalse(by_market["US_FUT"]["real_instrument_master"])

    def test_market_coverage_reports_missing_cn_provider(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token=None,
            demo_publish_url=None,
            demo_api_key=None,
            enable_scheduler=False,
            daily_provider="auto",
            openai_api_key=None,
            openai_model="model",
            signal_track_api_key=None,
        )

        with patch("signal_track.provider_diagnostics.find_spec", return_value=None):
            coverage = market_data_coverage(settings, "auto")

        by_market = {row["market"]: row for row in coverage["markets"]}
        self.assertFalse(by_market["CN_A"]["price_available"])
        self.assertIn("TUSHARE_TOKEN", by_market["CN_A"]["notes"][1])
        self.assertFalse(by_market["HK_FUT"]["price_available"])
        self.assertFalse(by_market["US_FUT"]["price_available"])

    def test_cli_self_check_runs_non_destructive_smoke_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                tushare_token=None,
                demo_publish_url=None,
                demo_api_key=None,
                enable_scheduler=False,
                daily_provider="fixture",
                openai_api_key=None,
                openai_model="model",
                signal_track_api_key=None,
            )
            html_path = Path(tmp) / "self-check.html"

            result = run_self_check(settings, provider_name="fixture", out=str(html_path))

            self.assertTrue(result["ok"])
            self.assertTrue(result["temporary_db"])
            self.assertEqual(result["resolved_symbols"], ["00700.HK"])
            self.assertEqual(result["checked_projects"], 1)
            self.assertTrue(html_path.exists())

    def test_source_name_can_be_inferred_from_content_marker(self) -> None:
        self.assertEqual(resolve_source_name(None, "信息源：Alpha Desk\n00700.HK 做多"), "Alpha Desk")
        self.assertEqual(resolve_source_name("manual", "来源：Beta\nAAPL 做空"), "Beta")
        self.assertIsNone(resolve_source_name("manual", "00700.HK 做多"))

    def test_ingest_low_logic_signal_creates_tracking_project_with_system_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试信息源",
                content="腾讯 做多，先跟踪。",
            )

            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertEqual(len(result.project_ids), 1)
            self.assertLess(result.logic_score, 6)
            self.assertTrue(result.system_logic_added)

            checked = DailyChecker(repo, FixtureMarketDataProvider()).run(next_fixture_trading_day(date.today()))
            self.assertEqual(checked, 1)

            html = render_dashboard(repo)
            self.assertIn("Signal Track 投资信号看板", html)
            self.assertIn("腾讯控股", html)
            self.assertIn("needs_review", html)
            self.assertIn("polyline", html)
            self.assertIn("系统补充逻辑", html)
            self.assertIn("项目检查日志", html)
            self.assertIn("needs_review", html)
            self.assertIn(next_fixture_trading_day(date.today()).isoformat(), html)

    def test_low_logic_signal_uses_optional_logic_supplementer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(
                repo,
                InstrumentResolver(repo.list_instruments()),
                logic_supplementer=FakeLogicSupplementer(),
            ).ingest(source_name="测试源", content="腾讯 做多，先跟踪。")

            logic = repo.list_logic_blocks(result.project_ids[0])
            system_block = [block for block in logic if block["logic_type"] == "system_logic"][0]
            system_logic = system_block["content"]
            evidence = json.loads(system_block["evidence"])
            self.assertIn("AI补充", system_logic)
            self.assertIn("关键跟踪指标", system_logic)
            self.assertIn("跌破 20 日线", system_logic)
            self.assertIn("tracking_metric: 广告收入环比改善", evidence)
            self.assertIn("exit_condition: 跌破 20 日线", evidence)
            self.assertIn("verification_note: 财务和行业数据需要外部来源交叉验证", evidence)

            research_items = repo.list_research_items(project_id=result.project_ids[0])
            research_by_type = {item["item_type"]: item["content"] for item in research_items}
            self.assertIn("tracking_metric", research_by_type)
            self.assertIn("exit_condition", research_by_type)
            self.assertIn("verification_note", research_by_type)
            updated = repo.update_research_item(
                int(research_items[0]["id"]),
                status="verified",
                source_note="checked public filings",
                metadata={"source": "manual"},
            )
            self.assertIsNotNone(updated)
            self.assertEqual(updated["status"], "verified")
            self.assertEqual(updated["source_note"], "checked public filings")

            html = render_dashboard(repo)
            self.assertIn("Evidence / verification", html)
            self.assertIn("研究验证项", html)
            self.assertIn("tracking_metric: 广告收入环比改善", html)
            self.assertIn("广告收入环比改善", html)

    def test_logic_supplementer_failure_falls_back_to_local_system_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(
                repo,
                InstrumentResolver(repo.list_instruments()),
                logic_supplementer=BrokenLogicSupplementer(),
            ).ingest(source_name="测试源", content="腾讯 做多，先跟踪。")

            logic = repo.list_logic_blocks(result.project_ids[0])
            system_block = [block for block in logic if block["logic_type"] == "system_logic"][0]
            system_logic = system_block["content"]
            evidence = json.loads(system_block["evidence"])
            self.assertIn("3C-5M-3D-3T", system_logic)
            self.assertIn("verification_status: unverified", evidence)
            research_items = repo.list_research_items(project_id=result.project_ids[0])
            self.assertGreaterEqual(len(research_items), 7)
            item_types = {item["item_type"] for item in research_items}
            self.assertIn("verification_note", item_types)
            self.assertIn("tracking_metric", item_types)
            self.assertIn("exit_condition", item_types)
            self.assertEqual(research_items[0]["item_type"], "verification_note")
            self.assertEqual(research_items[0]["status"], "unverified")
            self.assertTrue(any("at least two independent sources" in item["content"] for item in research_items))
            self.assertTrue(any("moving-average break" in item["content"] for item in research_items))
            metadata = [json.loads(item["metadata"] or "{}") for item in research_items]
            self.assertTrue(any(item.get("framework") == "Step 1" for item in metadata))

    def test_dashboard_groups_projects_by_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            ingestor.ingest(source_name="信息源A", content="腾讯 做多，先跟踪。")
            ingestor.ingest(source_name="信息源B", content="英伟达 做多，观察订单。")

            html = render_dashboard(repo)

            self.assertIn("source-summary", html)
            self.assertIn("data-source-filter", html)
            self.assertIn("table-wrap", html)
            self.assertIn("source-chip", html)
            self.assertIn("data-source='信息源A'", html)
            self.assertIn("class='card detail-card' data-source='信息源B'", html)
            self.assertIn("setFilter", html)
            self.assertIn("信息源A", html)
            self.assertIn("信息源B", html)
            self.assertIn("待复核", html)
            self.assertIn("尚未发布", html)
            self.assertNotIn("Futuristic minimalism", html)

    def test_structured_extraction_can_create_portfolio_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            extraction = ExtractedInput(
                signals=[
                    ExtractedSignal(
                        instruments=["宁德时代", "贵州茅台"],
                        direction="long",
                        source_logic="组合做多，分别跟踪电池和白酒龙头。",
                        observation_logic="观察订单、毛利率和消费复苏。",
                        logic_score=5,
                        is_portfolio=True,
                        weights={"300750.SZ": 0.6, "600519.SH": 0.4},
                    )
                ],
                source_name="结构化测试源",
            )

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="manual",
                content="组合：宁德时代 60%，贵州茅台 40%。",
                extraction=extraction,
            )

            self.assertEqual(len(result.project_ids), 1)
            self.assertEqual(result.resolved_symbols, ["300750.SZ", "600519.SH"])
            rows = repo.list_project_rows()
            self.assertEqual(rows[0]["symbols"], "300750.SZ, 600519.SH")

            DailyChecker(repo, FixtureMarketDataProvider()).run(next_fixture_trading_day(date.today()))
            performance = project_performance(repo, result.project_ids[0])
            self.assertTrue(all(leg.points for leg in performance.legs))
            self.assertTrue(all(leg.price_points for leg in performance.legs))
            html = render_dashboard(repo)
            self.assertIn("leg-curves", html)
            self.assertIn("mini-chart", html)
            self.assertIn("300750.SZ 价格曲线", html)
            self.assertIn("600519.SH 价格曲线", html)
            self.assertNotIn("300750.SZ 收益曲线", html)

    def test_heuristic_portfolio_weight_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试源",
                content="组合做多：宁德时代 60%，贵州茅台 40%，观察毛利率和消费恢复。",
                as_portfolio=True,
            )

            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.6)
            self.assertAlmostEqual(weights["600519.SH"], 0.4)

    def test_heuristic_portfolio_can_be_inferred_from_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
            )

            self.assertEqual(len(result.project_ids), 1)
            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.6)
            self.assertAlmostEqual(weights["600519.SH"], 0.4)
            self.assertFalse(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))

    def test_partial_portfolio_weights_default_to_equal_and_require_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ 60%, 600519.SH, watch margin and demand.",
            )

            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(sum(weights.values()), 1.0)
            self.assertAlmostEqual(weights["300750.SZ"], 0.5)
            self.assertAlmostEqual(weights["600519.SH"], 0.5)
            self.assertTrue(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))
            logic = repo.list_logic_blocks(result.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "system_logic" for block in logic))

    def test_plain_multi_instrument_note_still_splits_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Split Desk",
                content="300750.SZ long, watch battery margin. 600519.SH long, watch demand recovery.",
            )

            self.assertEqual(len(result.project_ids), 2)
            self.assertEqual(result.resolved_symbols, ["300750.SZ", "600519.SH"])

    def test_auto_portfolio_does_not_update_overlapping_single_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            single = ingestor.ingest(
                source_name="Overlap Desk",
                content="300750.SZ long, watch battery margin.",
            )
            portfolio = ingestor.ingest(
                source_name="Overlap Desk",
                content="portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
            )

            self.assertNotEqual(single.project_ids, portfolio.project_ids)
            self.assertEqual(len(repo.list_project_rows()), 2)
            self.assertEqual(len(repo.list_project_legs(portfolio.project_ids[0])), 2)

    def test_same_source_portfolio_followup_updates_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            opened = ingestor.ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
            )
            updated = ingestor.ingest(
                source_name="Portfolio Desk",
                content="portfolio update: 300750.SZ and 600519.SH, keep watching margin recovery.",
            )

            self.assertEqual(updated.project_ids, opened.project_ids)
            self.assertEqual(len(repo.list_project_rows()), 1)
            logic = repo.list_logic_blocks(opened.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "source_update" for block in logic))

    def test_update_tracking_project_weights_normalizes_and_clears_review_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ, 600519.SH, watch margin and demand.",
            )
            self.assertTrue(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))

            updated = update_tracking_project_weights(
                repo,
                result.project_ids[0],
                {"300750.SZ": 70, "600519.SH": 30},
                note="user supplied portfolio weights",
            )

            self.assertIsNotNone(updated)
            self.assertFalse(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))
            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.7)
            self.assertAlmostEqual(weights["600519.SH"], 0.3)
            logic = repo.list_logic_blocks(result.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "weight_update" for block in logic))

            with self.assertRaises(ProjectActionError) as ctx:
                update_tracking_project_weights(repo, result.project_ids[0], {"300750.SZ": 1.0})
            self.assertEqual(ctx.exception.code, "incomplete_weights")

    def test_close_signal_updates_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            opened = ingestor.ingest("测试源", "腾讯 做多，观察广告和游戏恢复。")
            closed = ingestor.ingest("测试源", "腾讯 平仓，游戏复苏低于预期。")

            self.assertEqual(closed.project_ids, opened.project_ids)
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "closed")
            logic = repo.list_logic_blocks(opened.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "close_logic" for block in logic))

    def test_close_signal_only_closes_same_source_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            source_a = ingestor.ingest("Source A", "00700.HK long, watch ads recovery.")
            source_b = ingestor.ingest("Source B", "00700.HK long, watch games recovery.")
            closed = ingestor.ingest("Source A", "00700.HK close, ads recovery failed.")

            self.assertEqual(closed.project_ids, source_a.project_ids)
            self.assertEqual(repo.get_project_row(source_a.project_ids[0])["status"], "closed")
            self.assertIn(repo.get_project_row(source_b.project_ids[0])["status"], {"active", "needs_review"})

    def test_unmatched_close_signal_does_not_create_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "No Position Source",
                "00700.HK close, no longer tracking.",
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertEqual(repo.list_project_rows(), [])
            self.assertEqual(len(repo.list_raw_inputs()), 1)

    def test_heuristic_plain_instrument_mention_does_not_create_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Background Source",
                "00700.HK earnings released without a trade signal.",
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertFalse(result.system_logic_added)
            self.assertEqual(repo.list_project_rows(), [])
            self.assertEqual(len(repo.list_raw_inputs()), 1)

    def test_heuristic_no_instrument_without_intent_does_not_create_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Background Source",
                "market color only, no actionable trade signal today.",
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, [])
            self.assertFalse(result.system_logic_added)
            self.assertEqual(repo.list_project_rows(), [])
            self.assertEqual(len(repo.list_raw_inputs()), 1)

    def test_heuristic_open_note_with_exit_condition_creates_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Alpha Desk",
                "00700.HK long, watch ads recovery and game pipeline. Exit if price breaks 20 day moving average.",
            )

            self.assertEqual(len(result.project_ids), 1)
            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertTrue(result.system_logic_added)
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["direction"], "long")
            self.assertIn(rows[0]["status"], {"active", "needs_review"})

    def test_heuristic_short_note_with_exit_condition_creates_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Short Book",
                "NVDA short, watch capex slowdown and margin pressure. Close if order data reaccelerates.",
            )

            self.assertEqual(len(result.project_ids), 1)
            self.assertEqual(result.resolved_symbols, ["NVDA"])
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["direction"], "short")

    def test_same_source_followup_updates_existing_project_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            opened = ingestor.ingest("测试源", "腾讯 做多，观察广告和游戏恢复。")
            updated = ingestor.ingest("测试源", "腾讯 做多更新，广告恢复速度低于预期，继续观察。")
            other_source = ingestor.ingest("另一个源", "腾讯 做多，观察广告恢复。")

            self.assertEqual(updated.project_ids, opened.project_ids)
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 2)
            logic = repo.list_logic_blocks(opened.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "source_update" for block in logic))
            self.assertNotEqual(other_source.project_ids, opened.project_ids)

    def test_structured_close_signal_updates_existing_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            opened = ingestor.ingest(
                "测试源",
                "腾讯 做多，观察广告和游戏恢复。",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["腾讯"],
                            direction="long",
                            source_logic="腾讯 做多，观察广告和游戏恢复。",
                            observation_logic="观察广告和游戏恢复。",
                            logic_score=7,
                            action="open",
                        )
                    ],
                ),
            )

            closed = ingestor.ingest(
                "测试源",
                "腾讯 平仓，广告低于预期。",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["腾讯"],
                            direction="neutral",
                            source_logic="腾讯 平仓，广告低于预期。",
                            observation_logic="",
                            logic_score=6,
                            action="close",
                        )
                    ],
                ),
            )

            self.assertEqual(closed.project_ids, opened.project_ids)
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["status"], "closed")
            logic = repo.list_logic_blocks(opened.project_ids[0])
            self.assertTrue(any(block["logic_type"] == "close_logic" for block in logic))

    def test_structured_close_signal_only_closes_same_source_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            source_a = ingestor.ingest("Source A", "00700.HK long, watch ads recovery.")
            source_b = ingestor.ingest("Source B", "00700.HK long, watch games recovery.")

            closed = ingestor.ingest(
                "Source A",
                "00700.HK close, ads recovery failed.",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["00700.HK"],
                            direction="neutral",
                            source_logic="00700.HK close, ads recovery failed.",
                            observation_logic="",
                            logic_score=7,
                            action="close",
                        )
                    ],
                ),
            )

            self.assertEqual(closed.project_ids, source_a.project_ids)
            self.assertEqual(repo.get_project_row(source_a.project_ids[0])["status"], "closed")
            self.assertIn(repo.get_project_row(source_b.project_ids[0])["status"], {"active", "needs_review"})

    def test_structured_unmatched_close_signal_does_not_create_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "No Position Source",
                "00700.HK close, no longer tracking.",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["00700.HK"],
                            direction="neutral",
                            source_logic="00700.HK close, no longer tracking.",
                            observation_logic="",
                            logic_score=7,
                            action="close",
                        )
                    ],
                ),
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertEqual(repo.list_project_rows(), [])
            self.assertEqual(len(repo.list_raw_inputs()), 1)

    def test_structured_open_signal_is_not_closed_by_other_signal_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Mixed Source",
                "00700.HK open long, watch ads recovery. NVDA close, order signal failed.",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["00700.HK"],
                            direction="long",
                            source_logic="00700.HK open long, watch ads recovery.",
                            observation_logic="watch ads recovery",
                            logic_score=7,
                            action="open",
                        ),
                        ExtractedSignal(
                            instruments=["NVDA"],
                            direction="neutral",
                            source_logic="NVDA close, order signal failed.",
                            observation_logic="",
                            logic_score=7,
                            action="close",
                        ),
                    ],
                ),
            )

            self.assertEqual(result.project_ids, [1])
            self.assertEqual(result.resolved_symbols, ["00700.HK", "NVDA"])
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbols"], "00700.HK")
            self.assertEqual(rows[0]["direction"], "long")

    def test_structured_none_action_does_not_create_tracking_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Background Source",
                "00700.HK quarterly earnings summary only.",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["00700.HK"],
                            direction="neutral",
                            source_logic="background only",
                            observation_logic="",
                            logic_score=2,
                            action="none",
                        )
                    ],
                ),
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, ["00700.HK"])
            self.assertFalse(result.system_logic_added)
            self.assertEqual(repo.list_project_rows(), [])
            self.assertEqual(len(repo.list_raw_inputs()), 1)

    def test_closed_project_prices_refresh_during_post_close_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            opened = ingestor.ingest("测试源", "腾讯 做多，观察广告。")
            repo.close_project(opened.project_ids[0], date(2026, 6, 5).isoformat())
            provider = RecordingMarketDataProvider("fixture")

            checked = DailyChecker(repo, provider).run(date(2026, 6, 20))

            self.assertEqual(checked, 0)
            self.assertIn("00700.HK", provider.calls)
            self.assertEqual(repo.count_price_bars("00700.HK"), 1)
            self.assertEqual(repo.list_daily_checks(project_id=opened.project_ids[0]), [])
            late_provider = RecordingMarketDataProvider("fixture")
            late_checked = DailyChecker(repo, late_provider).run(date(2026, 7, 10))
            self.assertEqual(late_checked, 0)
            self.assertEqual(late_provider.calls, [])

    def test_extract_published_address(self) -> None:
        body = '{"address":"https://example.com/demo/a","title":"x"}'
        self.assertEqual(extract_published_address(body), "https://example.com/demo/a")
        self.assertIsNone(extract_published_address("not json"))

    def test_scheduler_records_published_address(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)

            with patch("signal_track.scheduler.DemoPublisher", FakeDemoPublisher):
                checked = execute_daily_check(
                    repo,
                    provider=None,
                    publish_url="https://example.com/api/publish",
                    api_key="key",
                )

            events = repo.list_publish_events()
            self.assertEqual(checked, 0)
            self.assertEqual(events[0]["url"], "https://example.com/demo/signal")
            self.assertEqual(events[0]["status_code"], 200)

    def test_cli_ingest_auto_publishes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", FakeDemoPublisher):
                    with redirect_stdout(StringIO()):
                        code = cli_main([
                            "ingest",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "00700.HK long, watch ads recovery.",
                        ])

            self.assertEqual(code, 0)
            events = Repository(Database(db_path)).list_publish_events()
            self.assertEqual(events[0]["url"], "https://example.com/demo/signal")

    def test_cli_no_publish_disables_auto_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", FakeDemoPublisher):
                    with redirect_stdout(StringIO()):
                        code = cli_main([
                            "ingest",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "00700.HK long, watch ads recovery.",
                            "--no-publish",
                        ])

            self.assertEqual(code, 0)
            events = Repository(Database(db_path)).list_publish_events()
            self.assertEqual(events, [])

    def test_cli_ingest_auto_extractor_uses_openai_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "openai-key",
                "SIGNAL_TRACK_OPENAI_MODEL": "test-model",
            }
            FakeOpenAIExtractor.calls = []
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.OpenAISignalExtractor", FakeOpenAIExtractor):
                    with redirect_stdout(StringIO()):
                        code = cli_main([
                            "ingest",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "Use structured extraction for this note.",
                        ])

            repo = Repository(Database(db_path))
            projects = repo.list_project_rows()
            self.assertEqual(code, 0)
            self.assertEqual(FakeOpenAIExtractor.calls[0]["api_key"], "openai-key")
            self.assertEqual(FakeOpenAIExtractor.calls[0]["model"], "test-model")
            self.assertEqual(projects[0]["symbols"], "NVDA")
            self.assertEqual(projects[0]["direction"], "short")

    def test_cli_update_project_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(StringIO()):
                    create_code = cli_main([
                        "ingest",
                        "--source",
                        "CLI Desk",
                        "--text",
                        "portfolio long: 300750.SZ, 600519.SH, watch margin.",
                    ])
                repo = Repository(Database(db_path))
                project_id = int(repo.list_project_rows()[0]["id"])
                with redirect_stdout(StringIO()):
                    update_code = cli_main([
                        "update-project-weights",
                        str(project_id),
                        "--weights-json",
                        '{"300750.SZ": 65, "600519.SH": 35}',
                    ])

            legs = Repository(Database(db_path)).list_project_legs(project_id)
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertEqual(create_code, 0)
            self.assertEqual(update_code, 0)
            self.assertAlmostEqual(weights["300750.SZ"], 0.65)
            self.assertAlmostEqual(weights["600519.SH"], 0.35)

    def test_daily_logic_evaluator_can_trigger_exit_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试源",
                content="腾讯 做多，观察广告恢复和游戏流水。",
            )

            evaluator = FakeDailyEvaluator()
            DailyChecker(repo, FixtureMarketDataProvider(), evaluator=evaluator).run(next_fixture_trading_day(date.today()))

            row = repo.get_project_row(result.project_ids[0])
            self.assertEqual(row["status"], "exit_signal")
            self.assertGreater(evaluator.research_item_count, 0)
            checks = repo.list_daily_checks(project_id=result.project_ids[0])
            self.assertIn("核心跟踪假设被证伪", checks[0]["summary"])
            self.assertIn("逻辑评估：核心假设被证伪", checks[0]["triggered_rules"])

    def test_daily_check_continues_when_one_price_refresh_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            tencent = ingestor.ingest("Desk A", "00700.HK long, watch ads recovery.")
            nvda = ingestor.ingest("Desk B", "NVDA long, watch order growth.")
            provider = PartiallyFailingMarketDataProvider({"NVDA"})

            checked = DailyChecker(repo, provider).run(next_fixture_trading_day(date.today()))

            self.assertEqual(checked, 2)
            self.assertEqual(provider.calls, ["00700.HK", "NVDA"])
            self.assertGreater(repo.count_price_bars("00700.HK"), 0)
            self.assertEqual(repo.count_price_bars("NVDA"), 0)
            tencent_check = repo.list_daily_checks(project_id=tencent.project_ids[0])[0]
            nvda_check = repo.list_daily_checks(project_id=nvda.project_ids[0])[0]
            self.assertIn(tencent_check["conclusion"], {"watch", "needs_review"})
            self.assertEqual(nvda_check["conclusion"], "needs_review")
            self.assertIn("行情刷新失败：NVDA - provider unavailable", nvda_check["triggered_rules"])
            self.assertIn("缺少行情数据：NVDA", nvda_check["triggered_rules"])

    def test_exit_signal_summaries_include_latest_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试源",
                content="腾讯 做多，观察广告恢复和游戏流水。",
            )

            DailyChecker(repo, FixtureMarketDataProvider(), evaluator=FakeDailyEvaluator()).run(
                next_fixture_trading_day(date.today())
            )
            signals = exit_signal_summaries(repo)

            self.assertEqual([item["id"] for item in signals], result.project_ids)
            self.assertEqual(signals[0]["action"], "exit_signal")
            self.assertEqual(signals[0]["latest_check"]["conclusion"], "exit_signal")

    def test_cli_list_exit_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            db = Database(db_path)
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="CLI Desk",
                content="腾讯 做多，观察广告恢复和游戏流水。",
            )
            DailyChecker(repo, FixtureMarketDataProvider(), evaluator=FakeDailyEvaluator()).run(
                next_fixture_trading_day(date.today())
            )
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }

            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(output):
                    code = cli_main(["list-exit-signals"])

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["exit_signals"][0]["action"], "exit_signal")
            self.assertEqual(payload["exit_signals"][0]["latest_check"]["conclusion"], "exit_signal")

    def test_daily_check_triggers_moving_average_break_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试源",
                content="腾讯 做多，观察是否跌破5日线。",
            )
            instrument = repo.get_instrument("00700.HK")
            self.assertIsNotNone(instrument)
            instrument_id = repo.upsert_instrument(instrument)
            closes = [100, 100, 100, 100, 80]
            bars = [
                DailyBar(
                    symbol="00700.HK",
                    provider_symbol="00700.HK",
                    date=date(2026, 6, index + 1),
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    provider="test",
                )
                for index, close in enumerate(closes)
            ]
            repo.upsert_bars(instrument_id, bars)

            DailyChecker(repo).run(date(2026, 6, 5))

            row = repo.get_project_row(result.project_ids[0])
            self.assertEqual(row["status"], "exit_signal")
            checks = repo.list_daily_checks(project_id=result.project_ids[0])
            self.assertIn("跌破 5 日均线", checks[0]["triggered_rules"])

    def test_daily_check_uses_contradicted_research_exit_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(
                repo,
                InstrumentResolver(repo.list_instruments()),
                logic_supplementer=FakeLogicSupplementer(),
            ).ingest(source_name="测试源", content="腾讯 做多，先跟踪。")
            exit_item = [
                item
                for item in repo.list_research_items(project_id=result.project_ids[0])
                if item["item_type"] == "exit_condition"
            ][0]
            repo.update_research_item(int(exit_item["id"]), status="contradicted")

            DailyChecker(repo, FixtureMarketDataProvider()).run(next_fixture_trading_day(date.today()))

            row = repo.get_project_row(result.project_ids[0])
            self.assertEqual(row["status"], "exit_signal")
            checks = repo.list_daily_checks(project_id=result.project_ids[0])
            self.assertIn("研究验证项被证伪", checks[0]["triggered_rules"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_ingest_requires_or_infers_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())

            missing = client.post("/api/inputs", json={"content": "00700.HK 做多，观察广告"})
            self.assertEqual(missing.status_code, 422)
            self.assertEqual(missing.json()["detail"]["code"], "source_required")

            inferred = client.post("/api/inputs", json={"content": "信息源：Alpha Desk\n00700.HK 做多，观察广告"})
            self.assertEqual(inferred.status_code, 200)
            self.assertEqual(inferred.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(inferred.json()["projects"][0]["action"], "track")
            self.assertEqual(inferred.json()["projects"][0]["direction"], "long")
            self.assertEqual(inferred.json()["projects"][0]["symbols"], ["00700.HK"])

            projects = client.get("/api/projects").json()
            self.assertEqual(projects[0]["source_name"], "Alpha Desk")
            inputs = client.get("/api/inputs").json()
            self.assertEqual(inputs[0]["source_name"], "Alpha Desk")
            self.assertIn("00700.HK", inputs[0]["content_preview"])
            self.assertGreater(inputs[0]["content_length"], 0)
            self.assertNotIn("content", inputs[0])
            input_detail = client.get(f"/api/inputs/{inputs[0]['id']}").json()
            self.assertIn("00700.HK 做多", input_detail["content"])
            self.assertEqual(client.get("/api/inputs/999999").status_code, 404)
            project_detail = client.get(f"/api/projects/{projects[0]['id']}").json()
            self.assertEqual(project_detail["research_items"][0]["item_type"], "verification_note")
            item_id = project_detail["research_items"][0]["id"]
            listed_items = client.get("/api/research-items", params={"project_id": projects[0]["id"]}).json()
            self.assertEqual(listed_items[0]["id"], item_id)
            updated_item = client.patch(
                f"/api/research-items/{item_id}",
                json={"status": "verified", "source_note": "manual verification"},
            )
            self.assertEqual(updated_item.status_code, 200)
            self.assertEqual(updated_item.json()["item"]["status"], "verified")
            self.assertFalse(updated_item.json()["publish"]["attempted"])
            self.assertIsNone(updated_item.json()["publish"]["url"])

            closed = client.post("/api/inputs", json={"source": "Alpha Desk", "content": "00700.HK 平仓，广告低于预期"})
            self.assertEqual(closed.status_code, 200)
            self.assertEqual(closed.json()["project_ids"], [projects[0]["id"]])
            self.assertEqual(closed.json()["projects"][0]["action"], "close")
            self.assertEqual(closed.json()["projects"][0]["status"], "closed")

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_exit_signals_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())
                created = client.post(
                    "/api/inputs",
                    json={"source": "API Desk", "content": "腾讯 做多，观察广告恢复和游戏流水。"},
                )
            repo = Repository(Database(db_path))
            DailyChecker(repo, FixtureMarketDataProvider(), evaluator=FakeDailyEvaluator()).run(
                next_fixture_trading_day(date.today())
            )

            signals = client.get("/api/exit-signals").json()

            self.assertEqual(signals[0]["id"], created.json()["project_ids"][0])
            self.assertEqual(signals[0]["action"], "exit_signal")
            self.assertEqual(signals[0]["latest_check"]["conclusion"], "exit_signal")

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_unmatched_close_signal_does_not_create_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())
                response = client.post(
                    "/api/inputs",
                    json={"source": "No Position Source", "content": "00700.HK close, no longer tracking."},
                )
                projects = client.get("/api/projects").json()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["project_ids"], [])
            self.assertEqual(response.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(projects, [])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_plain_instrument_mention_does_not_create_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())
                response = client.post(
                    "/api/inputs",
                    json={"source": "Background Source", "content": "00700.HK earnings released without a trade signal."},
                )
                projects = client.get("/api/projects").json()

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["project_ids"], [])
            self.assertEqual(response.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(response.json()["projects"], [])
            self.assertEqual(projects, [])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_close_project_endpoint_records_close_logic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())

            created = client.post(
                "/api/inputs",
                json={"source": "Manual Desk", "content": "00700.HK long, watch ads recovery."},
            )
            project_id = created.json()["project_ids"][0]
            closed = client.post(
                f"/api/projects/{project_id}/close",
                json={"closed_date": "2026-06-10", "reason": "manual exit after thesis broke"},
            )
            detail = client.get(f"/api/projects/{project_id}").json()

            self.assertEqual(closed.status_code, 200)
            self.assertEqual(closed.json()["project"]["status"], "closed")
            self.assertEqual(closed.json()["project"]["closed_date"], "2026-06-10")
            self.assertFalse(closed.json()["publish"]["attempted"])
            self.assertTrue(
                any(
                    block["logic_type"] == "close_logic"
                    and "manual exit after thesis broke" in block["content"]
                    for block in detail["logic_blocks"]
                )
            )
            self.assertEqual(client.post("/api/projects/999999/close", json={}).status_code, 404)
            self.assertEqual(
                client.post(
                    f"/api/projects/{project_id}/close",
                    json={"closed_date": "not-a-date"},
                ).status_code,
                400,
            )

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_update_project_weights_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())

            created = client.post(
                "/api/inputs",
                json={
                    "source": "Portfolio Desk",
                    "content": "portfolio long: 300750.SZ, 600519.SH, watch margin.",
                },
            )
            project_id = created.json()["project_ids"][0]
            updated = client.patch(
                f"/api/projects/{project_id}/weights",
                json={"weights": {"300750.SZ": 55, "600519.SH": 45}, "note": "confirmed weights"},
            )
            incomplete = client.patch(
                f"/api/projects/{project_id}/weights",
                json={"weights": {"300750.SZ": 100}},
            )
            detail = client.get(f"/api/projects/{project_id}").json()

            self.assertEqual(updated.status_code, 200)
            weights = {leg["symbol"]: leg["weight"] for leg in updated.json()["legs"]}
            self.assertAlmostEqual(weights["300750.SZ"], 0.55)
            self.assertAlmostEqual(weights["600519.SH"], 0.45)
            self.assertFalse(updated.json()["project"]["weight_needs_review"])
            self.assertFalse(updated.json()["publish"]["attempted"])
            self.assertEqual(incomplete.status_code, 400)
            self.assertEqual(incomplete.json()["detail"]["code"], "incomplete_weights")
            self.assertTrue(any(block["logic_type"] == "weight_update" for block in detail["logic_blocks"]))

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_file_ingest_preserves_same_named_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())
                first = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("note.md", b"00700.HK long, watch ads.", "text/markdown")},
                )
                second = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("note.md", b"NVDA long, watch orders.", "text/markdown")},
                )

            self.assertEqual(first.status_code, 200)
            self.assertEqual(second.status_code, 200)
            raw_inputs = Repository(Database(db_path)).list_raw_inputs()
            paths = [Path(row["attachment_path"]) for row in raw_inputs]
            self.assertEqual(len(paths), 2)
            self.assertEqual({path.name for path in paths}, {"note.md", "note-1.md"})
            self.assertEqual({path.read_text(encoding="utf-8") for path in paths}, {
                "00700.HK long, watch ads.",
                "NVDA long, watch orders.",
            })
            listed = client.get("/api/inputs").json()
            self.assertEqual(len(listed), 2)
            self.assertEqual({Path(row["attachment_path"]).name for row in listed}, {"note.md", "note-1.md"})

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_check_run_uses_configured_daily_provider_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "SIGNAL_TRACK_DAILY_PROVIDER": "fixture",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())
                created = client.post(
                    "/api/inputs",
                    json={"source": "Daily Desk", "content": "00700.HK long, watch ads recovery."},
                )
                project_id = created.json()["project_ids"][0]
                checked = client.post("/api/checks/run", json={})
                detail = client.get(f"/api/projects/{project_id}").json()
                stored_bars = Repository(Database(Path(tmp) / "signal_track.sqlite3")).count_price_bars("00700.HK")

            self.assertEqual(checked.status_code, 200)
            self.assertEqual(checked.json()["checked_projects"], 1)
            self.assertGreater(stored_bars, 0)
            self.assertTrue(detail["daily_checks"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_research_item_update_publishes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.DemoPublisher", FakeDemoPublisher):
                    client = TestClient(create_app())
                    created = client.post(
                        "/api/inputs",
                        json={"source": "测试源", "content": "腾讯 做多"},
                    )
                    project_id = created.json()["project_ids"][0]
                    item_id = client.get(f"/api/projects/{project_id}").json()["research_items"][0]["id"]
                    updated = client.patch(
                        f"/api/research-items/{item_id}",
                        json={"status": "contradicted", "run_check": True, "provider": "fixture"},
                    )
                    events = client.get("/api/publish/events").json()
                    project_detail = client.get(f"/api/projects/{project_id}").json()
                    manual_publish = client.post("/api/publish")

            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["checked_projects"], 1)
            self.assertTrue(updated.json()["publish"]["ok"])
            self.assertEqual(updated.json()["publish"]["url"], "https://example.com/demo/signal")
            self.assertEqual(updated.json()["publish"]["publish_url"], "https://example.com/api/publish")
            self.assertEqual(manual_publish.status_code, 200)
            self.assertEqual(manual_publish.json()["url"], "https://example.com/demo/signal")
            self.assertEqual(events[0]["url"], "https://example.com/demo/signal")
            self.assertEqual(project_detail["project"]["status"], "needs_review")

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_mutating_web_endpoints_require_api_key_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "secret-key",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())

            denied = client.post("/api/inputs", json={"source": "测试源", "content": "腾讯 做多"})
            self.assertEqual(denied.status_code, 401)
            denied_research = client.patch("/api/research-items/1", json={"status": "verified"})
            self.assertEqual(denied_research.status_code, 401)
            denied_close = client.post("/api/projects/1/close", json={})
            self.assertEqual(denied_close.status_code, 401)
            denied_weights = client.patch("/api/projects/1/weights", json={"weights": {"00700.HK": 1}})
            self.assertEqual(denied_weights.status_code, 401)

            allowed = client.post(
                "/api/inputs",
                headers={"X-Signal-Track-Key": "secret-key"},
                json={"source": "测试源", "content": "腾讯 做多"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["resolved_symbols"], ["00700.HK"])

            health = client.get("/health")
            self.assertEqual(health.status_code, 200)

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_market_coverage_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                client = TestClient(create_app())

            response = client.get("/api/market-data/coverage", params={"provider": "auto"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["provider"], "auto")
        self.assertEqual(
            {row["market"] for row in response.json()["markets"]},
            {"CN_A", "HK", "CN_FUT", "HK_FUT", "US", "US_FUT"},
        )


if __name__ == "__main__":
    unittest.main()
