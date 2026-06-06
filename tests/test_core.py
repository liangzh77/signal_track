from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from io import StringIO
from io import BytesIO
from unittest.mock import patch
from datetime import date, timedelta
from pathlib import Path
from zipfile import ZipFile

from signal_track.config import Settings
from signal_track.db import Database, Repository
from signal_track.checker import DailyChecker
from signal_track.cli import refresh_markets as cli_refresh_markets
from signal_track.cli import main as cli_main
from signal_track.cli import run_self_check
from signal_track.dashboard import render_dashboard
from signal_track.analytics import LegPerformance, ProjectPerformance, combine_weighted_points, project_performance
from signal_track.daily_evaluator import DailyEvaluation, DailyLogicEvaluator, OpenAIDailyLogicEvaluator
from signal_track.exit_signals import exit_signal_summaries
from signal_track.extraction import ExtractedInput, ExtractedSignal
from signal_track.instrument_master import InstrumentMasterService
from signal_track.input_summary import project_input_history
from signal_track.logic_supplement import LogicSupplement, LogicSupplementer, OpenAILogicSupplementer
from signal_track.market_smoke import market_data_smoke
from signal_track.market_data import MarketDataService
from signal_track.models import DailyBar, Direction, Instrument, Market
from signal_track.provider_diagnostics import market_data_coverage
from signal_track.publisher import extract_published_address, publish_payload
from signal_track.publisher import PublishResult
from signal_track.project_report import build_project_report, render_project_report_markdown
from signal_track.providers.auto import AutoMarketDataProvider
from signal_track.providers.base import MarketDataProvider
from signal_track.providers.factory import build_auto_provider
from signal_track.providers.factory import build_market_data_provider
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.providers.tushare_provider import TushareMarketDataProvider
from signal_track.providers.yfinance_provider import get_price_field
from signal_track.project_actions import ProjectActionError, add_project_logic_block, update_tracking_project_weights
from signal_track.resolver import InstrumentResolver, SEED_INSTRUMENTS
from signal_track.rules import evaluate_return_rules
from signal_track.signals import SignalIngestor
from signal_track.source_detection import remove_source_marker_lines, resolve_source_name

try:
    from fastapi.testclient import TestClient
    from signal_track.web_app import create_app
except Exception:
    TestClient = None
    create_app = None

from signal_track.scheduler import build_scheduler, execute_daily_check, scheduler_job_summaries


def next_fixture_trading_day(current: date) -> date:
    while current.weekday() >= 5:
        current += timedelta(days=1)
    return current


def minimal_docx_bytes(text: str) -> bytes:
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    )
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document.encode("utf-8"))
    return buffer.getvalue()


class FakePdfPage:
    def __init__(self, text: str):
        self.text = text

    def extract_text(self) -> str:
        return self.text


class FakePdfReader:
    def __init__(self, stream):
        del stream
        self.pages = [FakePdfPage("NVDA long, watch orders.")]


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


class FakeTushareFrame:
    def __init__(self, records: list[dict]):
        self.records = records

    def to_dict(self, orient: str):
        self.assert_orient(orient)
        return self.records

    @staticmethod
    def assert_orient(orient: str) -> None:
        if orient != "records":
            raise AssertionError(f"unexpected orient: {orient}")


class FakeTusharePro:
    def __init__(self):
        self.fut_daily_calls: list[dict[str, str]] = []

    def fut_mapping(self, ts_code: str, start_date: str, end_date: str):
        self.mapping_call = {"ts_code": ts_code, "start_date": start_date, "end_date": end_date}
        return FakeTushareFrame(
            [
                {"trade_date": "20260602", "mapping_ts_code": "CU2607.SHF"},
                {"trade_date": "20260601", "mapping_ts_code": "CU2606.SHF"},
            ]
        )

    def fut_daily(self, ts_code: str, start_date: str, end_date: str):
        self.fut_daily_calls.append({"ts_code": ts_code, "start_date": start_date, "end_date": end_date})
        close = 81000 if ts_code == "CU2607.SHF" else 80000
        return FakeTushareFrame(
            [
                {
                    "ts_code": ts_code,
                    "trade_date": start_date,
                    "open": close - 100,
                    "high": close + 200,
                    "low": close - 300,
                    "close": close,
                    "settle": close + 10,
                    "vol": 1000,
                    "oi": 2000,
                }
            ]
        )


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


class FailingDemoPublisher:
    def __init__(self, publish_url: str, api_key: str):
        self.publish_url = publish_url
        self.api_key = api_key

    def publish(self, title: str, html: str, feature: str = "", disabled: bool = False) -> PublishResult:
        del title, html, feature, disabled
        return PublishResult(False, 500, '{"error":"publish failed"}')


class ThrowingDemoPublisher:
    def __init__(self, publish_url: str, api_key: str):
        self.publish_url = publish_url
        self.api_key = api_key

    def publish(self, title: str, html: str, feature: str = "", disabled: bool = False) -> PublishResult:
        del title, html, feature, disabled
        raise RuntimeError("network exploded")


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


class BrokenOpenAIExtractor:
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    def extract(self, content: str, source_hint: str | None = None) -> ExtractedInput:
        del content, source_hint
        raise RuntimeError("openai package missing")


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


class RecordingResponses:
    calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        schema_name = kwargs["text"]["format"]["name"]
        if schema_name == "tracking_logic_supplement":
            payload = {
                "thesis": "联网补充后的跟踪逻辑。",
                "tracking_metrics": ["财务数据交叉验证", "行业份额变化", "最新新闻催化"],
                "exit_conditions": ["核心假设被证伪", "跌破关键均线"],
                "verification_notes": ["引用来源需保留并复核"],
                "confidence": 0.7,
            }
        else:
            payload = {
                "conclusion": "watch",
                "summary": "联网检查后维持观察。",
                "triggered_rules": ["未发现明确平仓触发"],
                "confidence": 0.6,
            }
        return types.SimpleNamespace(output_text=json.dumps(payload, ensure_ascii=False))


class RecordingOpenAIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.responses = RecordingResponses()


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
        self.assertFalse(settings.openai_web_research)
        self.assertEqual(settings.openai_web_search_context_size, "medium")

    def test_settings_can_enable_openai_web_research(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_OPENAI_WEB_RESEARCH": "true",
                "SIGNAL_TRACK_OPENAI_WEB_SEARCH_CONTEXT_SIZE": "high",
            }
            with patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()

        self.assertTrue(settings.openai_web_research)
        self.assertEqual(settings.openai_web_search_context_size, "high")

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

    def test_database_verify_and_restore_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            db = Database(db_path)
            db.init()
            Repository(db).upsert_instrument(SEED_INSTRUMENTS[0])
            backup_path = db.backup(Path(tmp) / "backup.sqlite3")

            Repository(db).upsert_instrument(SEED_INSTRUMENTS[1])
            verify_report = db.verify()
            with self.assertRaises(FileExistsError):
                db.restore(backup_path)

            restored_path = db.restore(backup_path, force=True)
            restored_repo = Repository(Database(restored_path))

            self.assertTrue(verify_report["ok"])
            self.assertEqual(verify_report["schema_version"], 2)
            self.assertEqual(verify_report["table_counts"]["instruments"], 2)
            self.assertIsNotNone(restored_repo.get_instrument("300750.SZ"))
            self.assertIsNone(restored_repo.get_instrument("600519.SH"))

    def test_cli_verify_and_restore_db_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            backup_path = Path(tmp) / "backup.sqlite3"
            env = {"SIGNAL_TRACK_DB_PATH": str(db_path)}
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(StringIO()):
                    self.assertEqual(cli_main(["init-db"]), 0)
                    self.assertEqual(cli_main(["seed-instruments"]), 0)
                    self.assertEqual(cli_main(["backup-db", "--out", str(backup_path)]), 0)
                with redirect_stdout(StringIO()):
                    self.assertEqual(cli_main(["--db", str(db_path), "restore-db", "--from", str(backup_path)]), 1)
                verify_output = StringIO()
                with redirect_stdout(verify_output):
                    verify_code = cli_main(["verify-db"])
                with redirect_stdout(StringIO()):
                    restore_code = cli_main(["restore-db", "--from", str(backup_path), "--force"])

        verify_payload = json.loads(verify_output.getvalue())
        self.assertEqual(verify_code, 0)
        self.assertEqual(restore_code, 0)
        self.assertTrue(verify_payload["ok"])
        self.assertGreaterEqual(verify_payload["table_counts"]["instruments"], len(SEED_INSTRUMENTS))

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

    def test_auto_provider_falls_back_to_yfinance_when_tushare_market_call_fails(self) -> None:
        tushare_provider = PartiallyFailingMarketDataProvider({"00700.HK", "AAPL"})
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
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "AAPL"), date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(tushare_provider.calls, ["00700.HK", "AAPL"])
        self.assertEqual(yfinance_provider.calls, ["00700.HK", "AAPL"])

    def test_tushare_continuous_future_uses_mapping_contracts(self) -> None:
        provider = TushareMarketDataProvider.__new__(TushareMarketDataProvider)
        provider.pro = FakeTusharePro()
        instrument = next(item for item in SEED_INSTRUMENTS if item.symbol == "CU.SHF")

        bars = provider.get_daily_bars(instrument, date(2026, 6, 1), date(2026, 6, 2))

        self.assertEqual(
            provider.pro.fut_daily_calls,
            [
                {"ts_code": "CU2607.SHF", "start_date": "20260602", "end_date": "20260602"},
                {"ts_code": "CU2606.SHF", "start_date": "20260601", "end_date": "20260601"},
            ],
        )
        self.assertEqual([bar.provider_symbol for bar in bars], ["CU2606.SHF", "CU2607.SHF"])
        self.assertEqual([bar.close for bar in bars], [80000.0, 81000.0])

    def test_auto_provider_falls_back_when_tushare_dependency_missing(self) -> None:
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

        with patch("signal_track.providers.factory.TushareMarketDataProvider", side_effect=RuntimeError("missing tushare")):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "NQ"), date(2026, 6, 1), date(2026, 6, 5))
        self.assertEqual(yfinance_provider.calls, ["00700.HK", "NQ"])
        with self.assertRaisesRegex(ValueError, "CN_A"):
            provider.get_daily_bars(SEED_INSTRUMENTS[0], date(2026, 6, 1), date(2026, 6, 5))

    def test_provider_factory_wraps_dependency_errors_as_value_errors(self) -> None:
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

        with patch("signal_track.providers.factory.TushareMarketDataProvider", side_effect=RuntimeError("missing tushare")):
            with self.assertRaisesRegex(ValueError, "missing tushare"):
                build_market_data_provider("tushare", settings)
        with patch("signal_track.providers.factory.YFinanceMarketDataProvider", side_effect=RuntimeError("missing yfinance")):
            with self.assertRaisesRegex(ValueError, "missing yfinance"):
                build_market_data_provider("yfinance", settings)

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
        self.assertEqual(by_market["HK"]["fallback_price_providers"], ["yfinance"])
        self.assertEqual(by_market["CN_FUT"]["instrument_master_provider"], "tushare")
        self.assertEqual(by_market["HK_FUT"]["price_provider"], "yfinance")
        self.assertEqual(by_market["HK_FUT"]["instrument_master_provider"], "seed_fallback")
        self.assertFalse(by_market["HK_FUT"]["real_instrument_master"])
        self.assertEqual(by_market["US"]["fallback_price_providers"], ["yfinance"])
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

    def test_fixture_market_coverage_marks_seed_master_not_real(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token=None,
            demo_publish_url=None,
            demo_api_key=None,
            enable_scheduler=False,
            daily_provider="fixture",
            openai_api_key=None,
            openai_model="model",
            signal_track_api_key=None,
        )

        coverage = market_data_coverage(settings, "fixture")

        self.assertTrue(all(row["price_available"] for row in coverage["markets"]))
        self.assertTrue(all(row["instrument_master_provider"] == "seed_fallback" for row in coverage["markets"]))
        self.assertTrue(all(not row["real_instrument_master"] for row in coverage["markets"]))

    def test_market_smoke_fetches_sample_bars_across_markets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            provider = RecordingMarketDataProvider("recording")

            result = market_data_smoke(
                repo,
                provider,
                days=5,
                end_date=date(2026, 6, 5),
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "recording")
        self.assertEqual(
            {row["market"] for row in result["markets"]},
            {"CN_A", "HK", "CN_FUT", "HK_FUT", "US", "US_FUT"},
        )
        self.assertEqual({row["bar_count"] for row in result["markets"]}, {1})
        self.assertEqual(provider.calls, ["300750.SZ", "00700.HK", "CU.SHF", "HSI", "AAPL", "ES"])

    def test_market_smoke_reports_provider_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = market_data_smoke(
                repo,
                PartiallyFailingMarketDataProvider({"HSI"}),
                markets=[Market.HK_FUT],
                end_date=date(2026, 6, 5),
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["markets"][0]["symbol"], "HSI")
        self.assertIn("provider unavailable", result["markets"][0]["error"])

    def test_cli_market_smoke_uses_fixture_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    [
                        "--db",
                        str(db_path),
                        "market-smoke",
                        "--provider",
                        "fixture",
                        "--market",
                        "US_FUT",
                        "--days",
                        "5",
                    ]
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["markets"][0]["market"], "US_FUT")
        self.assertEqual(payload["markets"][0]["symbol"], "ES")
        self.assertGreater(payload["markets"][0]["bar_count"], 0)

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
            self.assertEqual(result["checked_projects"], result["project_count"])
            self.assertGreaterEqual(result["project_count"], 5)
            self.assertTrue(all(result["scenario_results"].values()))
            self.assertTrue(result["scenario_results"]["requires_source"])
            self.assertTrue(result["scenario_results"]["multi_instrument_split"])
            self.assertTrue(result["scenario_results"]["portfolio_project"])
            self.assertTrue(html_path.exists())

    def test_source_name_can_be_inferred_from_content_marker(self) -> None:
        self.assertEqual(resolve_source_name(None, "信息源：Alpha Desk\n00700.HK 做多"), "Alpha Desk")
        self.assertEqual(resolve_source_name("manual", "来源：Beta\nAAPL 做空"), "Beta")
        self.assertIsNone(resolve_source_name("manual", "00700.HK 做多"))

    def test_inline_source_marker_preserves_body_after_separator(self) -> None:
        content = "信息源：Alpha Desk；00700.HK 做多，观察广告"

        self.assertEqual(resolve_source_name(None, content), "Alpha Desk")
        self.assertEqual(remove_source_marker_lines(content), "00700.HK 做多，观察广告")

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

    def test_project_report_exports_framework_and_verification_state(self) -> None:
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
            ).ingest(source_name="Report Desk", content="00700.HK long, watch ads recovery.")
            DailyChecker(repo, FixtureMarketDataProvider()).run(next_fixture_trading_day(date.today()))

            report = build_project_report(repo, result.project_ids[0])
            self.assertIsNotNone(report)
            assert report is not None
            markdown = render_project_report_markdown(report)

        self.assertIn("3C-5M-3D-3T", report["title"])
        self.assertEqual(report["project"]["source_name"], "Report Desk")
        self.assertEqual(report["project"]["direction"], "long")
        self.assertEqual(report["instruments"][0]["symbol"], "00700.HK")
        self.assertGreater(report["data_verification"]["pending_count"], 0)
        self.assertEqual(len(report["scorecard"]), 9)
        scorecard = {item["dimension"]: item["score"] for item in report["scorecard"]}
        self.assertTrue(all(1 <= score <= 10 for score in scorecard.values()))
        self.assertLess(scorecard["周期位置"], 10)
        self.assertIn("tracking_metric", {item["item_type"] for item in report["research_items"]})
        self.assertIn("3C-5M-3D-3T", markdown)
        self.assertIn("## 二、3C分析：投资哲学定位", markdown)
        self.assertIn("## 三、5M分析：企业价值拆解", markdown)
        self.assertIn("## 四、3D分析：股价驱动力", markdown)
        self.assertIn("## 五、3T分析：时间框架", markdown)
        self.assertIn("| 维度 | 评分 | 一句话理由 |", markdown)
        self.assertIn("关键跟踪指标", markdown)
        self.assertIn("数据来源与免责声明", markdown)
        self.assertIn("tracking_metric", markdown)
        self.assertIn("免责声明", markdown)

    def test_openai_logic_supplementer_can_force_web_search(self) -> None:
        RecordingResponses.calls = []
        fake_openai = types.SimpleNamespace(OpenAI=RecordingOpenAIClient)
        with patch.dict("sys.modules", {"openai": fake_openai}):
            supplementer = OpenAILogicSupplementer(
                "key",
                "gpt-5.5",
                web_research=True,
                web_search_context_size="high",
            )
            supplement = supplementer.supplement(
                name="腾讯控股",
                direction=Direction.LONG,
                source_logic="腾讯 做多，先跟踪。",
                instruments=[next(item for item in SEED_INSTRUMENTS if item.symbol == "00700.HK")],
            )

        self.assertEqual(supplement.thesis, "联网补充后的跟踪逻辑。")
        request = RecordingResponses.calls[0]
        self.assertEqual(request["tools"], [{"type": "web_search", "search_context_size": "high"}])
        self.assertEqual(request["tool_choice"], "required")

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
            self.assertIn("data-filter-group=\"source\"", html)
            self.assertIn("data-filter-group=\"status\"", html)
            self.assertIn("data-filter-group=\"direction\"", html)
            self.assertIn("table-wrap", html)
            self.assertIn("source-chip", html)
            self.assertIn("data-source='信息源A'", html)
            self.assertIn("data-status='needs_review'", html)
            self.assertIn("data-direction='long'", html)
            self.assertIn("data-filter-type='status' data-value='exit_signal'", html)
            self.assertIn("data-filter-type='direction' data-value='short'", html)
            self.assertIn("report-card", html)
            self.assertIn("report-body", html)
            self.assertIn("/api/projects/", html)
            self.assertIn("format=markdown", html)
            self.assertIn("投研报告 | 基于风和3C-5M-3D-3T框架", html)
            self.assertIn("免责声明", html)
            self.assertIn("framework-tag covered", html)
            self.assertIn("<span>pending</span>", html)
            self.assertIn("class='card detail-card' data-source='信息源B'", html)
            self.assertIn("applyFilters", html)
            self.assertIn("最新检查", html)
            self.assertIn("动作", html)
            self.assertIn("复核逻辑", html)
            self.assertIn("信息源A", html)
            self.assertIn("信息源B", html)
            self.assertIn("待复核", html)
            self.assertIn("尚未发布", html)
            self.assertNotIn("Futuristic minimalism", html)

    def test_dashboard_shows_recent_input_feed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            ingestor.ingest(source_name="Input Desk", content="00700.HK long, watch ads recovery.")
            ingestor.ingest(source_name="Input Desk", content="00700.HK close, ads failed.")

            html = render_dashboard(repo)

            self.assertIn("recent-inputs", html)
            self.assertIn("data-input-action='close'", html)
            self.assertIn("Input history", html)
            self.assertIn("Input Desk", html)
            self.assertIn("00700.HK", html)
            self.assertIn("projects 1", html)

    def test_project_input_history_scans_beyond_recent_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            opened = ingestor.ingest("History Desk", "00700.HK long, watch ads recovery.")

            for index in range(120):
                ingestor.ingest("Noise Desk", f"NVDA earnings released background note {index}.")

            history = project_input_history(repo, opened.project_ids[0])

            self.assertEqual([item["id"] for item in history], [opened.raw_input_id])
            self.assertEqual(history[0]["input_action"], "track")
            self.assertEqual(history[0]["source_name"], "History Desk")

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

    def test_portfolio_curve_carries_forward_missing_leg_dates(self) -> None:
        leg_a = LegPerformance(
            leg_id=1,
            symbol="A",
            name="A",
            direction="long",
            weight=0.6,
            entry_date=None,
            entry_price=None,
            latest_date=None,
            latest_price=None,
            return_pct=None,
            points=[("2026-06-01", 0.0), ("2026-06-02", 0.1), ("2026-06-03", 0.2)],
            price_points=[],
        )
        leg_b = LegPerformance(
            leg_id=2,
            symbol="B",
            name="B",
            direction="long",
            weight=0.4,
            entry_date=None,
            entry_price=None,
            latest_date=None,
            latest_price=None,
            return_pct=None,
            points=[("2026-06-01", 0.0), ("2026-06-03", -0.1)],
            price_points=[],
        )

        points = combine_weighted_points([leg_a, leg_b])

        self.assertEqual(points[0], ("2026-06-01", 0.0))
        self.assertAlmostEqual(points[1][1], 0.06)
        self.assertEqual(points[1][0], "2026-06-02")
        self.assertAlmostEqual(points[2][1], 0.08)

    def test_portfolio_current_return_normalizes_leg_weights_like_curve(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ 66%, 600519.SH 33%, watch margin and demand.",
            )
            project_id = result.project_ids[0]
            legs = repo.list_project_legs(project_id)
            instrument_ids = {leg["symbol"]: int(leg["instrument_id"]) for leg in legs}
            entry_day = date.today()
            latest_day = entry_day + timedelta(days=1)
            repo.upsert_bars(
                instrument_ids["300750.SZ"],
                [
                    DailyBar(
                        symbol="300750.SZ",
                        provider_symbol="300750.SZ",
                        date=entry_day,
                        open=100,
                        high=100,
                        low=100,
                        close=100,
                        provider="test",
                    ),
                    DailyBar(
                        symbol="300750.SZ",
                        provider_symbol="300750.SZ",
                        date=latest_day,
                        open=110,
                        high=110,
                        low=110,
                        close=110,
                        provider="test",
                    ),
                ],
            )
            repo.upsert_bars(
                instrument_ids["600519.SH"],
                [
                    DailyBar(
                        symbol="600519.SH",
                        provider_symbol="600519.SH",
                        date=entry_day,
                        open=100,
                        high=100,
                        low=100,
                        close=100,
                        provider="test",
                    ),
                    DailyBar(
                        symbol="600519.SH",
                        provider_symbol="600519.SH",
                        date=latest_day,
                        open=90,
                        high=90,
                        low=90,
                        close=90,
                        provider="test",
                    ),
                ],
            )

            performance = project_performance(repo, project_id, latest_day)

            self.assertAlmostEqual(performance.return_pct, (0.1 * (2 / 3)) + (-0.1 * (1 / 3)))
            self.assertAlmostEqual(performance.points[-1][1], performance.return_pct)

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
            html = render_dashboard(repo)
            self.assertIn("待复核 1", html)
            self.assertIn("<td>是</td>", html)

    def test_portfolio_does_not_treat_return_percentages_as_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ upside 20%, 600519.SH downside 10%, watch margin and demand.",
            )

            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.5)
            self.assertAlmostEqual(weights["600519.SH"], 0.5)
            self.assertTrue(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))

    def test_portfolio_ordered_weight_percentages_require_weight_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ, 600519.SH, weights 60%, 40%, watch margin and demand.",
            )

            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.6)
            self.assertAlmostEqual(weights["600519.SH"], 0.4)
            self.assertFalse(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))

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

    def test_add_project_logic_block_appends_manual_observation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Manual Desk",
                content="00700.HK long, watch ads recovery.",
            )

            updated = add_project_logic_block(
                repo,
                result.project_ids[0],
                "manual check: ad revenue recovered faster than expected",
                logic_type="manual_note",
                confidence=0.8,
                evidence=["checked company update"],
            )
            logic = repo.list_logic_blocks(result.project_ids[0])

            self.assertIsNotNone(updated)
            self.assertTrue(any(block["logic_type"] == "manual_note" for block in logic))
            self.assertTrue(any("ad revenue recovered" in block["content"] for block in logic))
            with self.assertRaises(ProjectActionError) as ctx:
                add_project_logic_block(repo, result.project_ids[0], "bad", logic_type="close_logic")
            self.assertEqual(ctx.exception.code, "invalid_logic_type")

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

    def test_single_leg_close_does_not_close_portfolio_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            single = ingestor.ingest("Desk", "300750.SZ long, watch battery margin.")
            portfolio = ingestor.ingest(
                "Desk",
                "portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
            )

            closed = ingestor.ingest("Desk", "300750.SZ close, margin signal failed.")

            self.assertEqual(closed.project_ids, single.project_ids)
            self.assertEqual(repo.get_project_row(single.project_ids[0])["status"], "closed")
            self.assertIn(repo.get_project_row(portfolio.project_ids[0])["status"], {"active", "needs_review"})

    def test_full_portfolio_close_closes_matching_portfolio_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            portfolio = ingestor.ingest(
                "Desk",
                "portfolio long: 300750.SZ 60%, 600519.SH 40%, watch margin and demand.",
            )

            closed = ingestor.ingest("Desk", "portfolio close: 300750.SZ and 600519.SH, thesis failed.")

            self.assertEqual(closed.project_ids, portfolio.project_ids)
            row = repo.get_project_row(portfolio.project_ids[0])
            metadata = json.loads(row["metadata"])
            self.assertEqual(row["status"], "closed")
            self.assertTrue(metadata["portfolio"])
            self.assertEqual(metadata["closed_by_signal"], True)
            self.assertIn("thesis failed", metadata["close_reason"])

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

    def test_heuristic_later_symbol_resolves_unresolved_project_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            unresolved = ingestor.ingest(
                "Alpha Desk",
                "MysteryCo long, track margin inflection and close if orders weaken.",
            )
            resolved = ingestor.ingest(
                "Alpha Desk",
                "00700.HK is the target name for the previous note.",
            )

            self.assertEqual(resolved.project_ids, unresolved.project_ids)
            self.assertEqual(resolved.input_action, "update")
            self.assertEqual(resolved.resolved_symbols, ["00700.HK"])
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbols"], "00700.HK")
            self.assertEqual(rows[0]["direction"], "long")
            self.assertEqual(rows[0]["raw_input_id"], unresolved.raw_input_id)
            metadata = json.loads(rows[0]["metadata"])
            self.assertEqual(metadata["raw_extract_status"], "resolved_later")
            self.assertEqual(metadata["resolved_by_raw_input_id"], resolved.raw_input_id)
            logic_types = [block["logic_type"] for block in repo.list_logic_blocks(unresolved.project_ids[0])]
            self.assertIn("source_update", logic_types)

    def test_structured_later_symbol_resolves_unresolved_project_without_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            unresolved = ingestor.ingest(
                "Structured Desk",
                "raw note one",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["MysteryCo"],
                            direction="long",
                            source_logic="Track operating leverage and order recovery.",
                            observation_logic="Exit if order recovery fails.",
                            logic_score=5,
                        )
                    ],
                ),
            )
            resolved = ingestor.ingest(
                "Structured Desk",
                "raw note two",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["NVDA"],
                            direction="long",
                            source_logic="NVDA is the MysteryCo target; track data-center growth.",
                            observation_logic="Exit if data-center growth decelerates.",
                            logic_score=7,
                        )
                    ],
                ),
            )

            self.assertEqual(resolved.project_ids, unresolved.project_ids)
            self.assertEqual(resolved.input_action, "update")
            self.assertEqual(resolved.resolved_symbols, ["NVDA"])
            rows = repo.list_project_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["symbols"], "NVDA")
            self.assertEqual(rows[0]["status"], "active")

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

    def test_structured_unresolved_close_signal_does_not_create_unknown_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "No Position Source",
                "Mystery Asset close, no longer tracking.",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["Not A Real Ticker"],
                            direction="neutral",
                            source_logic="Mystery Asset close, no longer tracking.",
                            observation_logic="",
                            logic_score=5,
                            action="close",
                        )
                    ],
                ),
            )

            self.assertEqual(result.project_ids, [])
            self.assertEqual(result.resolved_symbols, [])
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
            performance = project_performance(repo, opened.project_ids[0])
            self.assertEqual(performance.window_start, "2026-05-06")
            self.assertEqual(performance.window_end, "2026-07-06")
            late_provider = RecordingMarketDataProvider("fixture")
            late_checked = DailyChecker(repo, late_provider).run(date(2026, 7, 10))
            self.assertEqual(late_checked, 0)
            self.assertEqual(late_provider.calls, [])

    def test_extract_published_address(self) -> None:
        body = '{"address":"https://example.com/demo/a","title":"x"}'
        self.assertEqual(extract_published_address(body), "https://example.com/demo/a")
        self.assertEqual(extract_published_address('{"url":"https://example.com/demo/b"}'), "https://example.com/demo/b")
        self.assertEqual(
            extract_published_address('{"data":{"public_url":"https://example.com/demo/c"}}'),
            "https://example.com/demo/c",
        )
        self.assertIsNone(extract_published_address("not json"))

    def test_publish_payload_exposes_failure_body(self) -> None:
        payload = publish_payload(PublishResult(False, 500, '{"error":"publish failed"}'), "https://example.com/api")

        self.assertTrue(payload["attempted"])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["status_code"], 500)
        self.assertEqual(payload["publish_url"], "https://example.com/api")
        self.assertEqual(payload["error"], '{"error":"publish failed"}')
        self.assertEqual(payload["response_body"], '{"error":"publish failed"}')

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

    def test_scheduler_records_failed_publish_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)

            with patch("signal_track.scheduler.DemoPublisher", FailingDemoPublisher):
                checked = execute_daily_check(
                    repo,
                    provider=None,
                    publish_url="https://example.com/api/publish",
                    api_key="key",
                )

            events = repo.list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(checked, 0)
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertEqual(events[0]["status_code"], 500)
            self.assertFalse(metadata["ok"])
            self.assertEqual(metadata["job"], "daily_check")
            self.assertEqual(metadata["error"], '{"error":"publish failed"}')

    def test_scheduler_records_publish_exception_without_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)

            with patch("signal_track.scheduler.DemoPublisher", ThrowingDemoPublisher):
                checked = execute_daily_check(
                    repo,
                    provider=None,
                    publish_url="invalid-url",
                    api_key="key",
                )

            events = repo.list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(checked, 0)
            self.assertEqual(events[0]["url"], "invalid-url")
            self.assertIsNone(events[0]["status_code"])
            self.assertIn("network exploded", events[0]["response_body"])
            self.assertFalse(metadata["ok"])
            self.assertEqual(metadata["exception_type"], "RuntimeError")

    def test_scheduler_registers_asia_evening_and_us_morning_jobs(self) -> None:
        try:
            import apscheduler  # noqa: F401
        except ImportError:
            self.skipTest("APScheduler unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)

            scheduled = build_scheduler(repo)

        job_ids = {job.id for job in scheduled.scheduler.get_jobs()}
        self.assertEqual(job_ids, {"asia_evening_daily_check", "us_morning_daily_check"})
        summaries = scheduler_job_summaries(scheduled.scheduler)
        self.assertEqual({summary["id"] for summary in summaries}, job_ids)
        self.assertTrue(any("19" in summary["trigger"] for summary in summaries))
        self.assertTrue(any("7" in summary["trigger"] for summary in summaries))

    def test_scheduler_job_summaries_handles_scheduler_like_objects(self) -> None:
        fake_time = types.SimpleNamespace(isoformat=lambda: "2026-06-06T19:00:00+08:00")
        fake_scheduler = types.SimpleNamespace(
            get_jobs=lambda: [
                types.SimpleNamespace(id="asia_evening_daily_check", trigger="cron[hour='19', minute='0']", next_run_time=fake_time),
                types.SimpleNamespace(id="us_morning_daily_check", trigger="cron[hour='7', minute='0']", next_run_time=None),
            ]
        )

        summaries = scheduler_job_summaries(fake_scheduler)

        self.assertEqual(summaries[0]["id"], "asia_evening_daily_check")
        self.assertEqual(summaries[0]["next_run_time"], "2026-06-06T19:00:00+08:00")
        self.assertEqual(summaries[1]["id"], "us_morning_daily_check")
        self.assertIsNone(summaries[1]["next_run_time"])

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

    def test_cli_ingest_records_publish_exception_and_keeps_update(self) -> None:
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
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", ThrowingDemoPublisher):
                    with redirect_stdout(output):
                        code = cli_main([
                            "ingest",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "00700.HK long, watch ads recovery.",
                        ])

            payload = json.loads(output.getvalue())
            repo = Repository(Database(db_path))
            events = repo.list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(code, 1)
            self.assertTrue(payload["project_ids"])
            self.assertFalse(payload["published"])
            self.assertIsNone(payload["status_code"])
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertIn("network exploded", events[0]["response_body"])
            self.assertFalse(metadata["ok"])
            self.assertEqual(metadata["exception_type"], "RuntimeError")

    def test_cli_publish_dashboard_records_exception(self) -> None:
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
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", ThrowingDemoPublisher):
                    with redirect_stdout(output):
                        code = cli_main(["publish-dashboard"])

            payload = json.loads(output.getvalue())
            events = Repository(Database(db_path)).list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertIn("network exploded", payload["error"])
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertFalse(metadata["ok"])
            self.assertEqual(metadata["flow"], "publish-dashboard")
            self.assertEqual(metadata["exception_type"], "RuntimeError")

    def test_cli_daily_run_returns_publish_error_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            html_path = Path(tmp) / "dashboard.html"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", ThrowingDemoPublisher):
                    with redirect_stdout(output):
                        code = cli_main([
                            "daily-run",
                            "--provider",
                            "fixture",
                            "--out",
                            str(html_path),
                            "--publish",
                        ])

            payload = json.loads(output.getvalue())
            events = Repository(Database(db_path)).list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(code, 1)
            self.assertTrue(html_path.exists())
            self.assertFalse(payload["published"])
            self.assertIsNone(payload["status_code"])
            self.assertIn("network exploded", payload["error"])
            self.assertIn("network exploded", payload["response_body"])
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertEqual(metadata["flow"], "daily-run")
            self.assertEqual(metadata["exception_type"], "RuntimeError")

    def test_cli_serve_passes_global_db_path_to_app_factory_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "serve.sqlite3"
            calls: list[dict[str, object]] = []
            fake_uvicorn = types.SimpleNamespace(
                run=lambda target, **kwargs: calls.append(
                    {
                        "target": target,
                        "kwargs": kwargs,
                        "db_path": os.environ.get("SIGNAL_TRACK_DB_PATH"),
                    }
                )
            )

            with patch.dict("sys.modules", {"uvicorn": fake_uvicorn}):
                with patch.dict("os.environ", {"SIGNAL_TRACK_DB_PATH": ""}, clear=False):
                    code = cli_main([
                        "--db",
                        str(db_path),
                        "serve",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8765",
                    ])

            self.assertEqual(code, 0)
            self.assertEqual(calls[0]["target"], "signal_track.web_app:create_app")
            self.assertEqual(calls[0]["kwargs"], {"factory": True, "host": "127.0.0.1", "port": 8765})
            self.assertEqual(calls[0]["db_path"], str(db_path))

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

    def test_cli_list_and_show_inputs_include_project_links(self) -> None:
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
                    cli_main(["ingest", "--source", "CLI Input Desk", "--text", "00700.HK long, watch ads recovery."])
                with redirect_stdout(StringIO()):
                    cli_main(["ingest", "--source", "CLI Input Desk", "--text", "00700.HK close, ads failed."])
                list_output = StringIO()
                with redirect_stdout(list_output):
                    list_code = cli_main(["list-inputs", "--limit", "1"])
                input_id = json.loads(list_output.getvalue())["inputs"][0]["id"]
                show_output = StringIO()
                with redirect_stdout(show_output):
                    show_code = cli_main(["show-input", str(input_id)])

        listed = json.loads(list_output.getvalue())["inputs"][0]
        shown = json.loads(show_output.getvalue())["input"]
        self.assertEqual(list_code, 0)
        self.assertEqual(show_code, 0)
        self.assertEqual(listed["source_name"], "CLI Input Desk")
        self.assertEqual(len(listed["project_ids"]), 1)
        self.assertEqual(listed["input_action"], "close")
        self.assertEqual(listed["projects"][0]["symbols"], ["00700.HK"])
        self.assertEqual(listed["projects"][0]["action"], "close")
        self.assertEqual(shown["project_ids"], listed["project_ids"])
        self.assertEqual(shown["input_action"], "close")
        self.assertIn("00700.HK close", shown["content"])

    def test_cli_list_projects_includes_performance_and_filters(self) -> None:
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
                    cli_main(["ingest", "--source", "CLI Desk A", "--text", "00700.HK long, watch ads recovery."])
                    cli_main(["ingest", "--source", "CLI Desk B", "--text", "NVDA short, watch orders."])
                    cli_main([
                        "check",
                        "--provider",
                        "fixture",
                        "--date",
                        next_fixture_trading_day(date.today()).isoformat(),
                    ])
                output = StringIO()
                with redirect_stdout(output):
                    code = cli_main(["list-projects", "--source", "CLI Desk A", "--status", "needs_review"])
                short_output = StringIO()
                with redirect_stdout(short_output):
                    short_code = cli_main(["list-projects", "--direction", "short"])

        payload = json.loads(output.getvalue())
        short_payload = json.loads(short_output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(len(payload["projects"]), 1)
        project = payload["projects"][0]
        self.assertEqual(project["source_name"], "CLI Desk A")
        self.assertEqual(project["status"], "needs_review")
        self.assertIn("performance", project)
        self.assertGreater(project["performance"]["point_count"], 0)
        self.assertEqual(project["performance"]["legs"][0]["symbol"], "00700.HK")
        self.assertEqual(project["latest_check"]["conclusion"], "watch")
        self.assertEqual(project["next_action"], "review_logic")
        self.assertEqual(short_code, 0)
        self.assertEqual(len(short_payload["projects"]), 1)
        self.assertEqual(short_payload["projects"][0]["source_name"], "CLI Desk B")
        self.assertEqual(short_payload["projects"][0]["direction"], "short")

    def test_cli_export_project_report_outputs_markdown_and_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            report_path = Path(tmp) / "project-report.md"
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
                    cli_main(["ingest", "--source", "CLI Report Desk", "--text", "00700.HK long, watch ads recovery."])
                project_id = int(Repository(Database(db_path)).list_project_rows()[0]["id"])
                with redirect_stdout(StringIO()) as markdown_output:
                    markdown_code = cli_main(["export-project-report", str(project_id), "--out", str(report_path)])
                json_output = StringIO()
                with redirect_stdout(json_output):
                    json_code = cli_main(["export-project-report", str(project_id), "--format", "json"])
                report_exists = report_path.exists()
                report_content = report_path.read_text(encoding="utf-8") if report_exists else ""

        payload = json.loads(json_output.getvalue())
        self.assertEqual(markdown_code, 0)
        self.assertEqual(json_code, 0)
        self.assertTrue(report_exists)
        self.assertIn("3C-5M-3D-3T", report_content)
        self.assertIn("path", json.loads(markdown_output.getvalue()))
        self.assertEqual(payload["project"]["source_name"], "CLI Report Desk")
        self.assertEqual(payload["instruments"][0]["symbol"], "00700.HK")

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

    def test_cli_auto_extractor_falls_back_to_heuristic_when_openai_fails(self) -> None:
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
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.OpenAISignalExtractor", BrokenOpenAIExtractor):
                    with redirect_stdout(StringIO()):
                        code = cli_main([
                            "ingest",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "00700.HK long, watch ads recovery.",
                        ])

            projects = Repository(Database(db_path)).list_project_rows()
            self.assertEqual(code, 0)
            self.assertEqual(projects[0]["symbols"], "00700.HK")
            self.assertEqual(projects[0]["direction"], "long")

    def test_cli_forced_openai_extractor_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "openai-key",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.OpenAISignalExtractor", BrokenOpenAIExtractor):
                    with self.assertRaisesRegex(SystemExit, "OpenAI extractor failed"):
                        cli_main([
                            "ingest",
                            "--extractor",
                            "openai",
                            "--source",
                            "CLI Desk",
                            "--text",
                            "00700.HK long, watch ads recovery.",
                        ])

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

    def test_cli_add_project_note_appends_logic_block(self) -> None:
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
                        "CLI Note Desk",
                        "--text",
                        "00700.HK long, watch ads.",
                    ])
                repo = Repository(Database(db_path))
                project_id = int(repo.list_project_rows()[0]["id"])
                note_output = StringIO()
                with redirect_stdout(note_output):
                    note_code = cli_main([
                        "add-project-note",
                        str(project_id),
                        "--text",
                        "manual observation: ads data improved",
                        "--type",
                        "manual_note",
                        "--confidence",
                        "0.7",
                        "--evidence-json",
                        '["checked internal note"]',
                    ])

            payload = json.loads(note_output.getvalue())
            logic = Repository(Database(db_path)).list_logic_blocks(project_id)
            self.assertEqual(create_code, 0)
            self.assertEqual(note_code, 0)
            self.assertTrue(payload["ok"])
            self.assertTrue(any(block["logic_type"] == "manual_note" for block in logic))
            self.assertTrue(any("ads data improved" in block["content"] for block in logic))

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

    def test_openai_daily_evaluator_can_force_web_search(self) -> None:
        RecordingResponses.calls = []
        fake_openai = types.SimpleNamespace(OpenAI=RecordingOpenAIClient)
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="测试源",
                content="腾讯 做多，观察广告恢复和游戏流水。",
            )

            with patch.dict("sys.modules", {"openai": fake_openai}):
                evaluator = OpenAIDailyLogicEvaluator(
                    "key",
                    "gpt-5.5",
                    web_research=True,
                    web_search_context_size="low",
                )
                DailyChecker(repo, FixtureMarketDataProvider(), evaluator=evaluator).run(
                    next_fixture_trading_day(date.today())
                )

        request = RecordingResponses.calls[0]
        self.assertEqual(request["tools"], [{"type": "web_search", "search_context_size": "low"}])
        self.assertEqual(request["tool_choice"], "required")

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

    def test_daily_check_clears_transient_needs_review_after_prices_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            opened = ingestor.ingest(
                "Desk A",
                "00700.HK 做多，因为广告恢复，观察是否跌破5日线，营收利润改善，ROE PB 估值。",
            )
            project_id = opened.project_ids[0]
            check_date = next_fixture_trading_day(date.today())

            DailyChecker(repo).run(check_date)
            missing_price_project = repo.get_project_row(project_id)
            self.assertEqual(missing_price_project["status"], "needs_review")
            self.assertTrue(bool(missing_price_project["needs_review"]))

            DailyChecker(repo, FixtureMarketDataProvider()).run(check_date)

            recovered_project = repo.get_project_row(project_id)
            recovered_check = repo.list_daily_checks(project_id=project_id)[0]
            self.assertEqual(recovered_check["conclusion"], "watch")
            self.assertEqual(recovered_project["status"], "active")
            self.assertFalse(bool(recovered_project["needs_review"]))

    def test_daily_check_keeps_project_level_needs_review_after_prices_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            opened = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Desk A",
                "00700.HK long",
            )
            project_id = opened.project_ids[0]
            check_date = next_fixture_trading_day(date.today())

            DailyChecker(repo, FixtureMarketDataProvider()).run(check_date)

            project = repo.get_project_row(project_id)
            check = repo.list_daily_checks(project_id=project_id)[0]
            self.assertEqual(check["conclusion"], "watch")
            self.assertEqual(project["status"], "needs_review")
            self.assertTrue(bool(project["needs_review"]))

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
            self.assertIn("performance", signals[0])
            self.assertIn("return_pct", signals[0]["performance"])
            self.assertEqual(signals[0]["performance"]["legs"][0]["symbol"], "00700.HK")

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
            self.assertIn("performance", payload["exit_signals"][0])
            self.assertEqual(payload["exit_signals"][0]["performance"]["legs"][0]["symbol"], "00700.HK")

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

    def test_daily_check_triggers_english_moving_average_break_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="English Source",
                content="Tencent long. Exit if price breaks below 5 day moving average.",
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

    def test_return_rules_parse_english_stop_loss_and_take_profit(self) -> None:
        loss_performance = ProjectPerformance(
            project_id=1,
            return_pct=-0.081,
            latest_date="2026-06-05",
            points=[],
            legs=[],
            missing_price_symbols=[],
        )
        profit_performance = ProjectPerformance(
            project_id=1,
            return_pct=0.151,
            latest_date="2026-06-05",
            points=[],
            legs=[],
            missing_price_symbols=[],
        )

        loss_hits = evaluate_return_rules("stop loss 8%; drawdown 12%", loss_performance)
        profit_hits = evaluate_return_rules("take profit 15%; upside 20%", profit_performance)

        self.assertEqual([hit.rule_type for hit in loss_hits], ["return_drawdown"])
        self.assertEqual([hit.rule_type for hit in profit_hits], ["return_take_profit"])

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
    def test_web_inbox_page_exposes_text_and_file_ingestion(self) -> None:
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
                home = client.get("/")
                inbox = client.get("/inbox")

        self.assertEqual(home.status_code, 200)
        self.assertEqual(inbox.status_code, 200)
        self.assertIn("Signal Track Inbox", home.text)
        self.assertIn("id=\"text-form\"", home.text)
        self.assertIn("id=\"file-form\"", home.text)
        self.assertIn("Project Update", home.text)
        self.assertIn("id=\"project-id\"", home.text)
        self.assertIn("id=\"project-select\"", home.text)
        self.assertIn("id=\"refresh-projects\"", home.text)
        self.assertIn("id=\"auto-refresh-projects\"", home.text)
        self.assertIn("id=\"project-note-provider\"", home.text)
        self.assertIn("id=\"project-note-run-check\"", home.text)
        self.assertIn("id=\"submit-note\"", home.text)
        self.assertIn("id=\"submit-weights\"", home.text)
        self.assertIn("id=\"submit-close\"", home.text)
        self.assertIn("Research Verification", home.text)
        self.assertIn("id=\"research-item-select\"", home.text)
        self.assertIn("id=\"research-status\"", home.text)
        self.assertIn("id=\"research-source-note\"", home.text)
        self.assertIn("id=\"research-run-check\"", home.text)
        self.assertIn("id=\"submit-research\"", home.text)
        self.assertIn("Daily Operations", home.text)
        self.assertIn("id=\"check-provider\"", home.text)
        self.assertIn("id=\"check-date\"", home.text)
        self.assertIn("id=\"run-checks\"", home.text)
        self.assertIn("id=\"publish-dashboard\"", home.text)
        self.assertIn("id=\"refresh-health\"", home.text)
        self.assertIn("id=\"recent-inputs\"", home.text)
        self.assertIn("id=\"refresh-inputs\"", home.text)
        self.assertIn("Market Data", home.text)
        self.assertIn("id=\"market-provider\"", home.text)
        self.assertIn("id=\"market-name\"", home.text)
        self.assertIn("id=\"market-coverage\"", home.text)
        self.assertIn("id=\"market-smoke\"", home.text)
        self.assertIn("id=\"refresh-instruments\"", home.text)
        self.assertIn("fetch('/api/inputs'", home.text)
        self.assertIn("fetch('/api/inputs?limit=8'", home.text)
        self.assertIn("fetch('/api/inputs/file'", home.text)
        self.assertIn("fetch('/api/projects'", home.text)
        self.assertIn("/api/research-items", home.text)
        self.assertIn("/api/checks/run", home.text)
        self.assertIn("/api/publish", home.text)
        self.assertIn("/api/market-data/coverage", home.text)
        self.assertIn("/api/market-data/smoke", home.text)
        self.assertIn("/api/instruments/refresh", home.text)
        self.assertIn("/health", home.text)
        self.assertIn("loadResearchItems()", home.text)
        self.assertIn("loadProjects()", home.text)
        self.assertIn("loadInputs()", home.text)
        self.assertIn("renderInputItem", home.text)
        self.assertIn("logic-blocks", home.text)
        self.assertIn("projectNoteRunCheckInput.checked", home.text)
        self.assertIn("projectNoteProviderInput.value", home.text)
        self.assertIn("/weights", home.text)
        self.assertIn("/close", home.text)
        self.assertIn("Authorization: `Bearer ${key}`", home.text)
        self.assertIn("/dashboard", home.text)

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

            inferred = client.post("/api/inputs", json={"content": "信息源：Alpha Desk；00700.HK 做多，观察广告"})
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
            self.assertEqual(inputs[0]["project_ids"], [projects[0]["id"]])
            self.assertEqual(inputs[0]["projects"][0]["symbols"], ["00700.HK"])
            self.assertEqual(inputs[0]["projects"][0]["action"], "track")
            self.assertNotIn("content", inputs[0])
            input_detail = client.get(f"/api/inputs/{inputs[0]['id']}").json()
            self.assertIn("00700.HK 做多", input_detail["content"])
            self.assertEqual(input_detail["project_ids"], [projects[0]["id"]])
            self.assertEqual(input_detail["projects"][0]["title"], projects[0]["title"])
            self.assertEqual(client.get("/api/inputs/999999").status_code, 404)
            project_detail = client.get(f"/api/projects/{projects[0]['id']}").json()
            self.assertEqual(project_detail["source_input"]["id"], inputs[0]["id"])
            self.assertIn("00700.HK 做多", project_detail["source_input"]["content"])
            self.assertEqual(project_detail["source_input"]["project_ids"], [projects[0]["id"]])
            self.assertEqual(len(project_detail["input_history"]), 1)
            self.assertEqual(project_detail["input_history"][0]["input_action"], "track")
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
            self.assertEqual(closed.json()["input_action"], "close")
            self.assertEqual(closed.json()["projects"][0]["action"], "close")
            self.assertEqual(closed.json()["projects"][0]["status"], "closed")
            inputs_after_close = client.get("/api/inputs").json()
            self.assertEqual(inputs_after_close[0]["project_ids"], [projects[0]["id"]])
            self.assertEqual(inputs_after_close[0]["input_action"], "close")
            self.assertEqual(inputs_after_close[0]["projects"][0]["action"], "close")
            close_detail = client.get(f"/api/inputs/{inputs_after_close[0]['id']}").json()
            self.assertEqual(close_detail["project_ids"], [projects[0]["id"]])
            self.assertEqual(close_detail["input_action"], "close")
            project_after_close = client.get(f"/api/projects/{projects[0]['id']}").json()
            self.assertEqual(
                [item["input_action"] for item in project_after_close["input_history"]],
                ["close", "track"],
            )

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_projects_list_includes_performance_and_filters(self) -> None:
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
                client.post("/api/inputs", json={"source": "Desk A", "content": "00700.HK long, watch ads recovery."})
                client.post("/api/inputs", json={"source": "Desk B", "content": "NVDA short, watch orders."})
                check_date = next_fixture_trading_day(date.today()).isoformat()
                checked = client.post("/api/checks/run", json={"provider": "fixture", "date": check_date})
                projects = client.get("/api/projects").json()
                desk_a = client.get("/api/projects", params={"source": "Desk A"}).json()
                active = client.get("/api/projects", params={"status": "needs_review"}).json()
                long_projects = client.get("/api/projects", params={"direction": "long"}).json()
                short_projects = client.get("/api/projects", params={"direction": "short"}).json()
                invalid_direction = client.get("/api/projects", params={"direction": "sideways"})

        self.assertEqual(checked.status_code, 200)
        self.assertEqual(len(projects), 2)
        self.assertIn("performance", projects[0])
        self.assertIn("return_pct", projects[0]["performance"])
        self.assertIn("points", projects[0]["performance"])
        self.assertGreater(projects[0]["performance"]["point_count"], 0)
        self.assertEqual(len(projects[0]["performance"]["legs"]), 1)
        self.assertEqual(projects[0]["latest_check"]["conclusion"], "watch")
        self.assertIn(projects[0]["next_action"], {"review_logic", "review_exit", "keep_tracking"})
        self.assertEqual([project["source_name"] for project in desk_a], ["Desk A"])
        self.assertEqual(len(active), 2)
        self.assertEqual([project["source_name"] for project in long_projects], ["Desk A"])
        self.assertEqual([project["source_name"] for project in short_projects], ["Desk B"])
        self.assertEqual(invalid_direction.status_code, 422)

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_project_report_endpoint_returns_markdown_and_json(self) -> None:
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
                ingested = client.post(
                    "/api/inputs",
                    json={"source": "Web Report Desk", "content": "00700.HK long, watch ads recovery."},
                )
                project_id = ingested.json()["project_ids"][0]
                markdown = client.get(f"/api/projects/{project_id}/report")
                report_json = client.get(f"/api/projects/{project_id}/report", params={"format": "json"})
                invalid = client.get(f"/api/projects/{project_id}/report", params={"format": "pdf"})
                missing = client.get("/api/projects/999999/report")

        self.assertEqual(markdown.status_code, 200)
        self.assertIn("text/markdown", markdown.headers["content-type"])
        self.assertIn("3C-5M-3D-3T", markdown.text)
        self.assertEqual(report_json.status_code, 200)
        self.assertEqual(report_json.json()["project"]["source_name"], "Web Report Desk")
        self.assertEqual(report_json.json()["instruments"][0]["symbol"], "00700.HK")
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(missing.status_code, 404)

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_auto_extractor_falls_back_when_openai_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "openai-key",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.OpenAISignalExtractor", BrokenOpenAIExtractor):
                    client = TestClient(create_app())
                    response = client.post(
                        "/api/inputs",
                        json={
                            "source": "Web Desk",
                            "content": "00700.HK long, watch ads recovery.",
                            "extractor": "auto",
                        },
                    )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(response.json()["projects"][0]["direction"], "long")

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_forced_openai_extractor_reports_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "openai-key",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.OpenAISignalExtractor", BrokenOpenAIExtractor):
                    client = TestClient(create_app())
                    response = client.post(
                        "/api/inputs",
                        json={
                            "source": "Web Desk",
                            "content": "00700.HK long, watch ads recovery.",
                            "extractor": "openai",
                        },
                    )

            self.assertEqual(response.status_code, 503)
            self.assertIn("OpenAI extractor failed", response.json()["detail"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_rejects_unknown_extractor(self) -> None:
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
                response = client.post(
                    "/api/inputs",
                    json={
                        "source": "Web Desk",
                        "content": "00700.HK long, watch ads recovery.",
                        "extractor": "typo",
                    },
                )

            repo = Repository(Database(db_path))
            self.assertEqual(response.status_code, 400)
            self.assertIn("Unknown extractor", response.json()["detail"])
            self.assertEqual(repo.list_raw_inputs(), [])
            self.assertEqual(repo.list_project_rows(), [])

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
    def test_web_add_project_logic_block_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "SIGNAL_TRACK_DB_PATH": str(Path(tmp) / "signal_track.sqlite3"),
                "SIGNAL_TRACK_API_KEY": "",
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
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
            added = client.post(
                f"/api/projects/{project_id}/logic-blocks",
                json={
                    "logic_type": "manual_note",
                    "content": "manual observation: ads trend improved",
                    "confidence": 0.75,
                    "evidence": ["checked ad tracker"],
                    "run_check": True,
                    "provider": "fixture",
                },
            )
            invalid = client.post(
                f"/api/projects/{project_id}/logic-blocks",
                json={"logic_type": "close_logic", "content": "bad"},
            )

            self.assertEqual(added.status_code, 200)
            self.assertEqual(added.json()["checked_projects"], 1)
            self.assertFalse(added.json()["publish"]["attempted"])
            self.assertTrue(any(block["logic_type"] == "manual_note" for block in added.json()["logic_blocks"]))
            self.assertTrue(any("ads trend improved" in block["content"] for block in added.json()["logic_blocks"]))
            self.assertTrue(added.json()["daily_checks"])
            self.assertEqual(invalid.status_code, 400)
            self.assertEqual(invalid.json()["detail"]["code"], "invalid_logic_type")

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
    def test_web_file_ingest_decodes_cjk_text_and_rejects_binary_files(self) -> None:
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
                cjk = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("note.txt", "腾讯 做多，观察广告。".encode("gb18030"), "text/plain")},
                )
                utf16 = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("utf16.txt", "NVDA long, watch orders.".encode("utf-16"), "text/plain")},
                )
                docx = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("note.docx", minimal_docx_bytes("00700.HK long, watch ads."), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
                )
                with patch.dict("sys.modules", {"pypdf": types.SimpleNamespace(PdfReader=FakePdfReader)}):
                    pdf = client.post(
                        "/api/inputs/file",
                        data={"source": "File Source"},
                        files={"file": ("note.pdf", b"%PDF-1.7\nfake", "application/pdf")},
                    )
                binary = client.post(
                    "/api/inputs/file",
                    data={"source": "File Source"},
                    files={"file": ("note.zip", b"PK\x03\x04binary", "application/zip")},
                )

            raw_inputs = Repository(Database(db_path)).list_raw_inputs()
            self.assertEqual(cjk.status_code, 200)
            self.assertEqual(cjk.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(utf16.status_code, 200)
            self.assertEqual(utf16.json()["resolved_symbols"], ["NVDA"])
            self.assertEqual(docx.status_code, 200)
            self.assertEqual(docx.json()["resolved_symbols"], ["00700.HK"])
            self.assertEqual(pdf.status_code, 200)
            self.assertEqual(pdf.json()["resolved_symbols"], ["NVDA"])
            self.assertEqual(binary.status_code, 415)
            self.assertEqual(binary.json()["detail"]["code"], "unsupported_input_file")
            self.assertEqual(len(raw_inputs), 4)
            attachment_paths = [Path(row["attachment_path"]) for row in raw_inputs]
            self.assertEqual(len(attachment_paths), 4)
            self.assertTrue(all(path.exists() for path in attachment_paths))
            self.assertNotIn("note.zip", {path.name for path in attachment_paths})
            self.assertTrue(any("腾讯" in row["content"] for row in raw_inputs))
            self.assertTrue(any("NVDA" in row["content"] for row in raw_inputs))
            self.assertTrue(any("watch ads" in row["content"] for row in raw_inputs))

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_file_ingest_cleans_attachment_when_source_missing(self) -> None:
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
                response = client.post(
                    "/api/inputs/file",
                    data={},
                    files={"file": ("missing-source.md", b"00700.HK long, watch ads.", "text/markdown")},
                )

            attachments = list((db_path.parent / "attachments").glob("*"))
            raw_inputs = Repository(Database(db_path)).list_raw_inputs()
            self.assertEqual(response.status_code, 422)
            self.assertEqual(response.json()["detail"]["code"], "source_required")
            self.assertEqual(raw_inputs, [])
            self.assertEqual(attachments, [])

    def test_cli_file_ingest_rejects_unsupported_binary_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            pdf_path = Path(tmp) / "note.zip"
            pdf_path.write_bytes(b"PK\x03\x04binary")
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(output):
                    code = cli_main(["ingest", "--source", "CLI File Source", "--file", str(pdf_path)])
            raw_inputs = Repository(Database(db_path)).list_raw_inputs()

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 4)
        self.assertEqual(payload["code"], "unsupported_input_file")
        self.assertEqual(raw_inputs, [])

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
            self.assertEqual(detail["summary"]["id"], project_id)
            self.assertEqual(detail["summary"]["source_name"], "Daily Desk")
            self.assertEqual(
                detail["summary"]["latest_check"]["conclusion"],
                detail["daily_checks"][0]["conclusion"],
            )
            self.assertIn(detail["summary"]["next_action"], {"review_logic", "review_exit", "keep_tracking"})
            self.assertIn("performance", detail["summary"])
            self.assertIn("point_count", detail["summary"]["performance"])
            self.assertIn("window_start", detail["summary"]["performance"])
            self.assertIn("window_end", detail["summary"]["performance"])
            self.assertIn("missing_price_symbols", detail["summary"]["performance"])
            self.assertEqual(detail["performance"]["window_start"], detail["summary"]["performance"]["window_start"])
            self.assertEqual(detail["performance"]["window_end"], detail["summary"]["performance"]["window_end"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_unknown_provider_returns_bad_request(self) -> None:
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
                check = client.post("/api/checks/run", json={"provider": "typo"})
                refresh = client.post("/api/instruments/refresh", json={"provider": "typo", "market": "CN_A"})

            repo = Repository(Database(db_path))
            self.assertEqual(check.status_code, 400)
            self.assertIn("Unknown market data provider", check.json()["detail"])
            self.assertEqual(refresh.status_code, 400)
            self.assertIn("Unknown market data provider", refresh.json()["detail"])
            self.assertEqual(repo.list_daily_checks(), [])

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
    def test_web_auto_publish_can_be_disabled_for_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.DemoPublisher", side_effect=AssertionError("unexpected auto publish")):
                    client = TestClient(create_app())
                    created = client.post(
                        "/api/inputs",
                        json={"source": "Web Desk", "content": "00700.HK long, watch ads recovery."},
                    )
                    events_after_update = client.get("/api/publish/events").json()
                with patch("signal_track.web_app.DemoPublisher", FakeDemoPublisher):
                    manual_publish = client.post("/api/publish")
                    events_after_manual = client.get("/api/publish/events").json()

        self.assertEqual(created.status_code, 200)
        self.assertFalse(created.json()["publish"]["attempted"])
        self.assertEqual(created.json()["publish"]["reason"], "auto publish disabled")
        self.assertEqual(events_after_update, [])
        self.assertEqual(manual_publish.status_code, 200)
        self.assertEqual(manual_publish.json()["url"], "https://example.com/demo/signal")
        self.assertEqual(events_after_manual[0]["url"], "https://example.com/demo/signal")

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_auto_publish_failure_is_returned_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.DemoPublisher", FailingDemoPublisher):
                    client = TestClient(create_app())
                    created = client.post(
                        "/api/inputs",
                        json={"source": "测试源", "content": "腾讯 做多"},
                    )
                    events = client.get("/api/publish/events").json()
                    health = client.get("/health").json()

            publish = created.json()["publish"]
            self.assertEqual(created.status_code, 200)
            self.assertTrue(publish["attempted"])
            self.assertFalse(publish["ok"])
            self.assertEqual(publish["status_code"], 500)
            self.assertEqual(publish["error"], '{"error":"publish failed"}')
            self.assertEqual(events[0]["status_code"], 500)
            self.assertEqual(events[0]["response_body"], '{"error":"publish failed"}')
            self.assertFalse(health["ok"])
            self.assertIn("latest_publish_failed", health["degraded_reasons"])
            self.assertFalse(health["latest_publish"]["ok"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_auto_publish_exception_is_returned_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.DemoPublisher", ThrowingDemoPublisher):
                    client = TestClient(create_app())
                    created = client.post(
                        "/api/inputs",
                        json={"source": "测试源", "content": "腾讯 做多"},
                    )
                    events = client.get("/api/publish/events").json()
                    health = client.get("/health").json()

            publish = created.json()["publish"]
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(created.status_code, 200)
            self.assertTrue(created.json()["project_ids"])
            self.assertTrue(publish["attempted"])
            self.assertFalse(publish["ok"])
            self.assertIsNone(publish["status_code"])
            self.assertIn("network exploded", publish["error"])
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertIn("network exploded", events[0]["response_body"])
            self.assertEqual(metadata["exception_type"], "RuntimeError")
            self.assertFalse(health["ok"])
            self.assertIn("latest_publish_failed", health["degraded_reasons"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_manual_publish_exception_is_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_API_KEY": "",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
                "OPENAI_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.web_app.DemoPublisher", ThrowingDemoPublisher):
                    client = TestClient(create_app())
                    response = client.post("/api/publish")
                    events = client.get("/api/publish/events").json()

            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(response.status_code, 502)
            self.assertIn("network exploded", response.json()["detail"])
            self.assertEqual(events[0]["url"], "https://example.com/api/publish")
            self.assertIn("network exploded", events[0]["response_body"])
            self.assertFalse(metadata["ok"])
            self.assertEqual(metadata["feature"], "Signal Track 手动发布")
            self.assertEqual(metadata["exception_type"], "RuntimeError")

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
            self.assertTrue(health.json()["ok"])

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_health_reports_operational_summary(self) -> None:
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
                client.post("/api/inputs", json={"source": "Health Desk", "content": "00700.HK long, watch ads."})
                check_date = next_fixture_trading_day(date.today()).isoformat()
                client.post("/api/checks/run", json={"provider": "fixture", "date": check_date})
                Repository(Database(db_path)).record_publish_event(
                    title="Signal Track 投资信号看板",
                    url="https://example.com/signal",
                    status_code=200,
                    response_body='{"address":"https://example.com/signal"}',
                    metadata={"ok": True, "flow": "test"},
                )
                response = client.get("/health")

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["degraded_reasons"], [])
        self.assertTrue(payload["database"]["ok"])
        self.assertFalse(payload["scheduler_enabled"])
        self.assertEqual(payload["scheduler_jobs"], [])
        self.assertEqual(payload["projects"]["total"], 1)
        self.assertEqual(payload["projects"]["needs_review"], 1)
        self.assertEqual(payload["latest_check"]["check_date"], check_date)
        self.assertEqual(payload["latest_publish"]["status_code"], 200)
        self.assertTrue(payload["latest_publish"]["ok"])
        self.assertEqual(payload["latest_publish"]["url"], "https://example.com/signal")

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

    @unittest.skipUnless(TestClient and create_app, "FastAPI test client unavailable")
    def test_web_market_smoke_endpoint_uses_fixture_provider(self) -> None:
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

            response = client.get(
                "/api/market-data/smoke",
                params={"provider": "fixture", "market": "HK_FUT", "days": 5},
            )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "fixture")
        self.assertEqual(payload["markets"][0]["market"], "HK_FUT")
        self.assertEqual(payload["markets"][0]["symbol"], "HSI")
        self.assertGreater(payload["markets"][0]["bar_count"], 0)


if __name__ == "__main__":
    unittest.main()
