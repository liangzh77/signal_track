from __future__ import annotations

import json
import os
import tempfile
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
from signal_track.daily_evaluator import DailyEvaluation, DailyLogicEvaluator
from signal_track.exit_signals import exit_signal_summaries
from signal_track.extraction import ExtractedInput, ExtractedSignal
from signal_track.instrument_master import InstrumentMasterService
from signal_track.input_summary import project_input_history
from signal_track.logic_supplement import LogicSupplement, LogicSupplementer
from signal_track.market_smoke import market_data_smoke
from signal_track.market_data import MarketDataService
from signal_track.models import AssetType, DailyBar, Direction, Instrument, Market
from signal_track.provider_diagnostics import market_data_coverage
from signal_track.publisher import extract_published_address, publish_payload
from signal_track.publisher import PublishResult
from signal_track.project_report import build_project_report, render_project_report_markdown
from signal_track.providers.auto import AutoMarketDataProvider
from signal_track.providers.base import MarketDataProvider
from signal_track.providers.factory import build_auto_provider
from signal_track.providers.factory import build_market_data_provider
from signal_track.providers.eastmoney_fund_provider import EastmoneyFundProvider, bars_from_payload, fund_code_for
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.providers.tushare_provider import TushareMarketDataProvider
from signal_track.providers.tushare_provider import to_float as tushare_to_float
from signal_track.providers.yfinance_provider import get_price_field, yfinance_symbol
from signal_track.project_actions import ProjectActionError, add_project_logic_block, update_tracking_project_weights
from signal_track.resolver import InstrumentResolver, SEED_INSTRUMENTS
from signal_track.rules import evaluate_return_rules, extract_percent_thresholds
from signal_track.signals import SignalIngestor, extract_probe_terms
from signal_track.source_detection import remove_source_marker_lines, resolve_source_name



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


class EmptyMarketDataProvider(RecordingMarketDataProvider):
    def __init__(self, name: str = "empty"):
        super().__init__(name)

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        del start_date, end_date, adjustment
        self.calls.append(instrument.symbol)
        return []


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
                "SIGNAL_TRACK_DAILY_PROVIDER": "",
                "TUSHARE_TOKEN": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
            }
            with patch.dict("os.environ", env, clear=True):
                settings = Settings.from_env()

        self.assertEqual(settings.daily_provider, "auto")

    def test_cli_doctor_reports_readiness_without_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_DAILY_PROVIDER": "auto",
                "TUSHARE_TOKEN": "",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=True):
                with patch("signal_track.provider_diagnostics.dependency_available", return_value=False):
                    with redirect_stdout(output):
                        code = cli_main(["doctor", "--provider", "auto"])

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertFalse(payload["ok"])
            self.assertTrue(payload["codex_first"])
            self.assertFalse(payload["backend_service_required"])
            self.assertFalse(payload["database"]["exists"])
            self.assertFalse(payload["configuration"]["demo_api_key_configured"])
            self.assertEqual(payload["market_data"]["price_ready_markets"], ["CN_A"])
            self.assertEqual(
                payload["market_data"]["missing_price_markets"],
                ["HK", "CN_FUT", "HK_FUT", "US", "US_FUT"],
            )
            self.assertIn("database file does not exist", payload["warnings"])
            self.assertFalse(db_path.exists())

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
            ("9868", None, "09868.HK", Market.HK),
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
        self.assertIsNone(resolver.resolve("2026"))
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

    def test_database_bar_storage_filters_non_finite_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            instrument_id = repo.upsert_instrument(SEED_INSTRUMENTS[0])

            repo.upsert_bars(
                instrument_id,
                [
                    DailyBar(
                        symbol="300750.SZ",
                        provider_symbol="300750.SZ",
                        date=date(2026, 1, 1),
                        open=float("nan"),
                        high=float("inf"),
                        low=99.0,
                        close=float("-inf"),
                        adj_close=100.0,
                        volume=float("nan"),
                        provider="test",
                    )
                ],
            )

            bar = repo.get_latest_price_bar("300750.SZ")
            self.assertIsNotNone(bar)
            assert bar is not None
            self.assertIsNone(bar["open"])
            self.assertIsNone(bar["high"])
            self.assertEqual(bar["low"], 99.0)
            self.assertIsNone(bar["close"])
            self.assertEqual(bar["adj_close"], 100.0)
            self.assertIsNone(bar["volume"])

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
            self.assertEqual(version, 3)
            with db.session() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracking_projects)")}
                research_columns = {row["name"] for row in conn.execute("PRAGMA table_info(research_items)")}
                report_columns = {row["name"] for row in conn.execute("PRAGMA table_info(project_reports)")}
            self.assertIn("logic_score", columns)
            self.assertIn("weight_needs_review", columns)
            self.assertIn("item_type", research_columns)
            self.assertIn("status", research_columns)
            self.assertIn("content_hash", report_columns)
            self.assertIn("generated_at", report_columns)

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
            self.assertEqual(verify_report["schema_version"], 3)
            self.assertEqual(verify_report["table_counts"]["instruments"], 2)
            self.assertEqual(verify_report["table_counts"]["project_reports"], 0)
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

    def test_cli_refresh_instruments_defaults_to_auto_provider(self) -> None:
        calls: list[str] = []

        def fake_build_provider(name: str, settings: Settings) -> MarketDataProvider:
            del settings
            calls.append(name)
            return RecordingMarketDataProvider(name, SEED_INSTRUMENTS)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            with patch("signal_track.cli.build_market_data_provider", side_effect=fake_build_provider):
                with redirect_stdout(StringIO()):
                    code = cli_main(["--db", str(db_path), "refresh-instruments", "--market", "CN_A"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, ["auto"])

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
            daily_provider="auto",
        )

        with patch("signal_track.providers.factory.TushareMarketDataProvider", return_value=tushare_provider):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        provider.get_daily_bars(SEED_INSTRUMENTS[0], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "HSI"), date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(SEED_INSTRUMENTS[-1], date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(tushare_provider.calls, ["300750.SZ", "00700.HK"])
        self.assertEqual(yfinance_provider.calls, ["HSI", "NQ"])

    def test_auto_provider_falls_back_to_yfinance_when_tushare_market_call_fails(self) -> None:
        tushare_provider = PartiallyFailingMarketDataProvider({"00700.HK", "AAPL"})
        yfinance_provider = RecordingMarketDataProvider("yfinance")
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
        )

        with patch("signal_track.providers.factory.TushareMarketDataProvider", return_value=tushare_provider):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "AAPL"), date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(tushare_provider.calls, ["00700.HK", "AAPL"])
        self.assertEqual(yfinance_provider.calls, ["00700.HK", "AAPL"])

    def test_auto_provider_falls_back_when_primary_returns_no_bars(self) -> None:
        tushare_provider = EmptyMarketDataProvider("tushare")
        yfinance_provider = RecordingMarketDataProvider("yfinance")
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
        )

        with patch("signal_track.providers.factory.TushareMarketDataProvider", return_value=tushare_provider):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        bars = provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(tushare_provider.calls, ["00700.HK"])
        self.assertEqual(yfinance_provider.calls, ["00700.HK"])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].provider, "yfinance")

    def test_auto_provider_uses_eastmoney_fund_for_open_fund_fallback(self) -> None:
        yfinance_provider = EmptyMarketDataProvider("yfinance")
        eastmoney_provider = RecordingMarketDataProvider("eastmoney_fund")
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token=None,
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
        )
        instrument = Instrument(
            symbol="006947.OF",
            provider_symbol="006947.OF",
            name="Open Fund",
            aliases=("006947",),
            market=Market.CN_A,
            asset_type=AssetType.ETF,
            exchange="OF",
            currency="CNY",
            timezone="Asia/Shanghai",
            metadata={"fund_type": "open_fund"},
        )

        with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
            with patch("signal_track.providers.factory.EastmoneyFundProvider", return_value=eastmoney_provider):
                provider = build_auto_provider(settings)

        bars = provider.get_daily_bars(instrument, date(2026, 6, 1), date(2026, 6, 5))

        self.assertEqual(yfinance_provider.calls, ["006947.OF"])
        self.assertEqual(eastmoney_provider.calls, ["006947.OF"])
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].provider, "eastmoney_fund")

    def test_eastmoney_fund_payload_parses_historical_nav_rows(self) -> None:
        instrument = Instrument(
            symbol="006947.OF",
            provider_symbol="006947.OF",
            name="Open Fund",
            aliases=("006947",),
            market=Market.CN_A,
            asset_type=AssetType.ETF,
            exchange="OF",
            currency="CNY",
            timezone="Asia/Shanghai",
            metadata={"fund_type": "open_fund"},
        )
        payload = {
            "Data": {
                "LSJZList": [
                    {"FSRQ": "2026-06-08", "DWJZ": "1.2227", "LJJZ": "1.2427"},
                    {"FSRQ": "2026-06-05", "DWJZ": "1.2229", "LJJZ": "1.2429"},
                    {"FSRQ": "2026-05-30", "DWJZ": "1.2200", "LJJZ": "1.2400"},
                ]
            }
        }

        bars = bars_from_payload(payload, instrument, "006947", date(2026, 6, 1), date(2026, 6, 8))

        self.assertEqual(fund_code_for(instrument), "006947")
        self.assertEqual([bar.date for bar in bars], [date(2026, 6, 5), date(2026, 6, 8)])
        self.assertEqual([bar.close for bar in bars], [1.2229, 1.2227])
        self.assertEqual([bar.adj_close for bar in bars], [1.2429, 1.2427])
        self.assertEqual([bar.provider for bar in bars], ["eastmoney_fund", "eastmoney_fund"])

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
            daily_provider="auto",
        )

        with patch("signal_track.providers.factory.TushareMarketDataProvider", side_effect=RuntimeError("missing tushare")):
            with patch("signal_track.providers.factory.YFinanceMarketDataProvider", return_value=yfinance_provider):
                provider = build_auto_provider(settings)

        provider.get_daily_bars(SEED_INSTRUMENTS[0], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(SEED_INSTRUMENTS[2], date(2026, 6, 1), date(2026, 6, 5))
        provider.get_daily_bars(next(item for item in SEED_INSTRUMENTS if item.symbol == "NQ"), date(2026, 6, 1), date(2026, 6, 5))
        self.assertEqual(yfinance_provider.calls, ["300750.SZ", "00700.HK", "NQ"])

    def test_provider_factory_wraps_dependency_errors_as_value_errors(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
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
        self.assertIsNone(get_price_field({"Close": float("nan")}, "AAPL", "Close"))
        self.assertIsNone(get_price_field({"Close": FakeSeries(float("inf"))}, "AAPL", "Close"))

    def test_tushare_price_field_rejects_non_finite_values(self) -> None:
        self.assertEqual(tushare_to_float("101.5"), 101.5)
        self.assertIsNone(tushare_to_float(""))
        self.assertIsNone(tushare_to_float(float("nan")))
        self.assertIsNone(tushare_to_float(float("inf")))

    def test_yfinance_symbol_normalizes_china_and_hong_kong_stock_codes(self) -> None:
        catl = next(instrument for instrument in SEED_INSTRUMENTS if instrument.symbol == "300750.SZ")
        maotai = next(instrument for instrument in SEED_INSTRUMENTS if instrument.symbol == "600519.SH")
        tencent = next(instrument for instrument in SEED_INSTRUMENTS if instrument.symbol == "00700.HK")
        alibaba = next(instrument for instrument in SEED_INSTRUMENTS if instrument.symbol == "09988.HK")
        hsi = next(instrument for instrument in SEED_INSTRUMENTS if instrument.symbol == "HSI")

        self.assertEqual(yfinance_symbol(catl), "300750.SZ")
        self.assertEqual(yfinance_symbol(maotai), "600519.SS")
        self.assertEqual(yfinance_symbol(tencent), "0700.HK")
        self.assertEqual(yfinance_symbol(alibaba), "9988.HK")
        self.assertEqual(yfinance_symbol(hsi), "HSI=F")

    def test_market_coverage_reports_auto_routes_without_remote_calls(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token="token",
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
        )

        with patch("signal_track.provider_diagnostics.find_spec", return_value=object()):
            coverage = market_data_coverage(settings, "auto")

        by_market = {row["market"]: row for row in coverage["markets"]}
        self.assertEqual(by_market["CN_A"]["price_provider"], "tushare")
        self.assertEqual(by_market["CN_A"]["fallback_price_providers"], ["yfinance", "eastmoney_fund"])
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
            daily_provider="auto",
        )

        with patch("signal_track.provider_diagnostics.find_spec", return_value=None):
            coverage = market_data_coverage(settings, "auto")

        by_market = {row["market"]: row for row in coverage["markets"]}
        self.assertTrue(by_market["CN_A"]["price_available"])
        self.assertEqual(by_market["CN_A"]["price_provider"], "eastmoney_fund")
        self.assertTrue(any("eastmoney_fund" in note for note in by_market["CN_A"]["notes"]))
        self.assertFalse(by_market["HK_FUT"]["price_available"])
        self.assertFalse(by_market["US_FUT"]["price_available"])

    def test_market_coverage_uses_yfinance_for_a_shares_without_tushare_token(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token=None,
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="auto",
        )

        with patch("signal_track.provider_diagnostics.find_spec", return_value=object()):
            coverage = market_data_coverage(settings, "auto")

        by_market = {row["market"]: row for row in coverage["markets"]}
        self.assertEqual(by_market["CN_A"]["price_provider"], "yfinance")
        self.assertEqual(by_market["CN_A"]["instrument_master_provider"], "seed_fallback")
        self.assertFalse(by_market["CN_A"]["real_instrument_master"])
        self.assertFalse(by_market["CN_FUT"]["price_available"])

    def test_fixture_market_coverage_marks_seed_master_not_real(self) -> None:
        settings = Settings(
            db_path=Path(":memory:"),
            tushare_token=None,
            demo_publish_url=None,
            demo_api_key=None,
            daily_provider="fixture",
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

    def test_cli_import_bars_loads_cn_future_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            csv_path = Path(tmp) / "cu.csv"
            csv_path.write_text(
                "trade_date,open,high,low,close,vol,amount,settle,oi\n"
                "20260601,80000,80500,79800,80300,1000,1200000,80200,50000\n"
                "20260602,80300,81000,80100,80900,1100,1300000,80800,50500\n",
                encoding="utf-8",
            )
            output = StringIO()

            with redirect_stdout(output):
                code = cli_main([
                    "--db",
                    str(db_path),
                    "import-bars",
                    "铜主连",
                    "--market",
                    "CN_FUT",
                    "--file",
                    str(csv_path),
                    "--provider",
                    "licensed-csv",
                    "--provider-symbol",
                    "CU.SHF",
                ])

            payload = json.loads(output.getvalue())
            repo = Repository(Database(db_path))
            latest = repo.get_latest_price_bar("CU.SHF")
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["symbol"], "CU.SHF")
            self.assertEqual(payload["stored_bar_count"], 2)
            self.assertEqual(payload["total_bar_count"], 2)
            self.assertEqual(payload["start"], "2026-06-01")
            self.assertEqual(payload["end"], "2026-06-02")
            self.assertIsNotNone(latest)
            self.assertEqual(latest["provider"], "licensed-csv")
            self.assertEqual(latest["close"], 80900.0)

    def test_cli_self_check_runs_non_destructive_smoke_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                db_path=Path(tmp) / "main.sqlite3",
                tushare_token=None,
                demo_publish_url=None,
                demo_api_key=None,
                    daily_provider="fixture",
                        )
            html_path = Path(tmp) / "self-check.html"

            result = run_self_check(settings, provider_name="fixture", out=str(html_path))

            self.assertTrue(result["ok"])
            self.assertTrue(result["temporary_db"])
            self.assertEqual(result["resolved_symbols"], ["00700.HK"])
            self.assertEqual(result["checked_projects"], result["active_project_count"])
            self.assertGreaterEqual(result["project_count"], 7)
            self.assertTrue(all(result["scenario_results"].values()))
            self.assertTrue(result["scenario_results"]["requires_source"])
            self.assertTrue(result["scenario_results"]["source_marker_inference"])
            self.assertTrue(result["scenario_results"]["multi_instrument_split"])
            self.assertTrue(result["scenario_results"]["portfolio_project"])
            self.assertTrue(result["scenario_results"]["portfolio_missing_weights_review"])
            self.assertTrue(result["scenario_results"]["close_signal"])
            self.assertTrue(result["scenario_results"]["market_coverage"])
            self.assertTrue(result["scenario_results"]["report_archive"])
            self.assertTrue(result["report_artifacts"])
            self.assertTrue(html_path.exists())
            self.assertIn("已归档报告：", html_path.read_text(encoding="utf-8"))

    def test_source_name_can_be_inferred_from_content_marker(self) -> None:
        self.assertEqual(resolve_source_name(None, "信息源：Alpha Desk\n00700.HK 做多"), "Alpha Desk")
        self.assertEqual(resolve_source_name("manual", "来源：Beta\nAAPL 做空"), "Beta")
        self.assertEqual(resolve_source_name(None, "信息来源：Gamma Desk\n00700.HK 做多"), "Gamma Desk")
        self.assertEqual(resolve_source_name(None, "信号源：Delta Desk\n00700.HK 做多"), "Delta Desk")
        self.assertEqual(resolve_source_name(None, "消息源：Echo Desk\n00700.HK 做多"), "Echo Desk")
        self.assertIsNone(resolve_source_name("manual", "00700.HK 做多"))

    def test_inline_source_marker_preserves_body_after_separator(self) -> None:
        content = "信息源：Alpha Desk；00700.HK 做多，观察广告"

        self.assertEqual(resolve_source_name(None, content), "Alpha Desk")
        self.assertEqual(remove_source_marker_lines(content), "00700.HK 做多，观察广告")

        comma_content = "信息源：Alpha Desk，00700.HK 做多，观察广告"
        self.assertEqual(resolve_source_name(None, comma_content), "Alpha Desk")
        self.assertEqual(remove_source_marker_lines(comma_content), "00700.HK 做多，观察广告")

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
            self.assertIn("投资信号看板", html)
            self.assertIn("腾讯控股", html)
            self.assertIn("needs_review", html)
            self.assertIn("polyline", html)
            self.assertIn("chart-marker-open", html)
            self.assertIn("开仓点：", html)
            self.assertIn("chart-marker-curve-start", html)
            self.assertIn("chart-marker-curve-end", html)
            self.assertIn("曲线开始点：", html)
            self.assertIn("曲线结束点：", html)
            self.assertNotIn("平仓点：", html)
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
            self.assertIn("证据 / 验证", html)
            self.assertIn("研究验证项", html)
            self.assertIn("跟踪指标：广告收入环比改善", html)
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
            self.assertIn("<th>入场</th>", html)
            self.assertIn("source-chip", html)
            self.assertIn('href="/inbox"', html)
            self.assertIn("class=\"top-actions\"", html)
            self.assertIn("data-source='信息源A'", html)
            self.assertIn("data-status='needs_review'", html)
            self.assertIn("data-direction='long'", html)
            self.assertIn("title='待复核'>待复核", html)
            self.assertIn("title='00700.HK / 腾讯控股'", html)
            self.assertIn("data-filter-type='status' data-value='exit_signal'", html)
            self.assertIn("data-filter-type='direction' data-value='short'", html)
            self.assertIn("report-card", html)
            self.assertIn("report-body", html)
            self.assertIn("data:text/markdown;charset=utf-8,", html)
            self.assertIn("aria-label='下载项目投研报告文件'", html)
            self.assertIn("download='signal-track-project-", html)
            self.assertNotIn("/api/projects/", html)
            self.assertIn("投研报告 | 基于风和3C-5M-3D-3T框架", html)
            self.assertIn("免责声明", html)
            self.assertIn("framework-tag covered", html)
            self.assertIn("<span>待验证</span>", html)
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
            ingestor.ingest(source_name="Mixed Desk", content="300750.SZ long, watch battery margin.")
            ingestor.ingest(
                source_name="Mixed Desk",
                content="300750.SZ long update, 600519.SH long, watch margin and demand.",
            )

            html = render_dashboard(repo)

            self.assertIn("recent-inputs", html)
            self.assertIn("data-input-action='close'", html)
            self.assertIn("data-input-action='mixed'", html)
            self.assertIn("input-action mixed", html)
            self.assertIn("输入记录", html)
            self.assertIn("Input Desk", html)
            self.assertIn("Mixed Desk", html)
            self.assertIn("00700.HK", html)
            self.assertIn("关联项目 1", html)

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
            self.assertIn("chart-marker-open", html)
            self.assertIn("chart-marker-curve-start", html)
            self.assertIn("chart-marker-curve-end", html)
            self.assertNotIn("平仓点：", html)
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

    def test_missing_portfolio_weights_mark_status_review_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            strong_logic = (
                "portfolio long: 300750.SZ and 600519.SH. Watch PE, PB, ROE, order recovery, "
                "margin trend, cash flow quality, valuation sentiment, policy changes, channel inventory, "
                "and exit if the original thesis is contradicted by verified data. Continue tracking revenue, "
                "profit margin, market share, management execution, free cash flow, leverage, and price behavior "
                "against the opening thesis across short, medium, and long-term checkpoints."
            )
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content=strong_logic,
                as_portfolio=True,
            )

            project = repo.get_project_row(result.project_ids[0])
            self.assertGreaterEqual(project["logic_score"], 6)
            self.assertFalse(bool(project["needs_review"]))
            self.assertTrue(bool(project["weight_needs_review"]))
            self.assertEqual(project["status"], "needs_review")

            update_tracking_project_weights(
                repo,
                result.project_ids[0],
                {"300750.SZ": 60, "600519.SH": 40},
            )
            updated = repo.get_project_row(result.project_ids[0])
            self.assertFalse(bool(updated["weight_needs_review"]))
            self.assertFalse(bool(updated["needs_review"]))
            self.assertEqual(updated["status"], "active")

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

    def test_portfolio_ordered_ratio_weights_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ, 600519.SH, weights 6:4, watch margin and demand.",
            )

            legs = repo.list_project_legs(result.project_ids[0])
            weights = {leg["symbol"]: leg["weight"] for leg in legs}
            self.assertAlmostEqual(weights["300750.SZ"], 0.6)
            self.assertAlmostEqual(weights["600519.SH"], 0.4)
            self.assertFalse(bool(repo.get_project_row(result.project_ids[0])["weight_needs_review"]))

    def test_portfolio_per_symbol_decimal_weights_are_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Portfolio Desk",
                content="portfolio long: 300750.SZ allocation 0.6, 600519.SH allocation 0.4, watch margin and demand.",
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

    def test_partial_overlap_multi_instrument_note_updates_and_creates_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            existing = ingestor.ingest(
                source_name="Overlap Desk",
                content="300750.SZ long, watch battery margin.",
            )
            mixed = ingestor.ingest(
                source_name="Overlap Desk",
                content="300750.SZ long update, 600519.SH long, watch margin and demand.",
            )

            rows = repo.list_project_rows()
            self.assertEqual(mixed.input_action, "mixed")
            self.assertEqual(len(mixed.project_ids), 2)
            self.assertIn(existing.project_ids[0], mixed.project_ids)
            self.assertEqual(len(rows), 2)
            self.assertEqual(mixed.resolved_symbols, ["300750.SZ", "600519.SH"])
            new_project_id = next(project_id for project_id in mixed.project_ids if project_id != existing.project_ids[0])
            new_legs = repo.list_project_legs(new_project_id)
            self.assertEqual([leg["symbol"] for leg in new_legs], ["600519.SH"])
            existing_logic_types = [block["logic_type"] for block in repo.list_logic_blocks(existing.project_ids[0])]
            self.assertIn("source_update", existing_logic_types)

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

            for bad_weight in (float("nan"), float("inf")):
                with self.assertRaises(ProjectActionError) as bad_ctx:
                    update_tracking_project_weights(
                        repo,
                        result.project_ids[0],
                        {"300750.SZ": bad_weight, "600519.SH": 1.0},
                    )
                self.assertEqual(bad_ctx.exception.code, "invalid_weight")

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
            for bad_confidence in (float("nan"), float("inf")):
                with self.assertRaises(ProjectActionError) as bad_ctx:
                    add_project_logic_block(
                        repo,
                        result.project_ids[0],
                        "bad confidence",
                        confidence=bad_confidence,
                    )
                self.assertEqual(bad_ctx.exception.code, "invalid_confidence")

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

    def test_closing_incomplete_weight_portfolio_clears_review_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))
            portfolio = ingestor.ingest(
                "Desk",
                "portfolio long: 300750.SZ and 600519.SH, watch margin and demand.",
            )

            before_close = repo.get_project_row(portfolio.project_ids[0])
            self.assertEqual(before_close["status"], "needs_review")
            self.assertTrue(bool(before_close["weight_needs_review"]))

            closed = ingestor.ingest("Desk", "portfolio close: 300750.SZ and 600519.SH, thesis failed.")

            self.assertEqual(closed.project_ids, portfolio.project_ids)
            row = repo.get_project_row(portfolio.project_ids[0])
            self.assertEqual(row["status"], "closed")
            self.assertFalse(bool(row["needs_review"]))
            self.assertFalse(bool(row["weight_needs_review"]))
            html = render_dashboard(repo)
            self.assertIn("待复核 0", html)

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

    def test_heuristic_plain_hk_numeric_code_creates_synthetic_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "HK Desk",
                "9868 做多，观察毛利率和订单恢复。",
            )

            self.assertEqual(result.resolved_symbols, ["09868.HK"])
            self.assertEqual(len(result.project_ids), 1)
            project = repo.list_project_rows()[0]
            self.assertEqual(project["symbols"], "09868.HK")
            instrument = repo.get_instrument("09868.HK")
            self.assertEqual(instrument.market, Market.HK)
            self.assertTrue(instrument.metadata["synthetic"])

    def test_heuristic_year_and_percentages_are_not_synthetic_hk_symbols(self) -> None:
        resolver = InstrumentResolver()
        terms = extract_probe_terms("2026 年收入增长 20%，止损 10%，观察 20 日线")

        self.assertNotIn("2026", terms)
        self.assertNotIn("20", terms)
        self.assertIsNone(resolver.resolve("2026"))

    def test_extract_probe_terms_keeps_china_future_contract_codes(self) -> None:
        resolver = InstrumentResolver()
        terms = extract_probe_terms("CU2601.SHF long, IF2606.CFX short, CU.SHF watch.")

        self.assertIn("CU2601.SHF", terms)
        self.assertIn("IF2606.CFX", terms)
        self.assertIn("CU.SHF", terms)
        self.assertNotIn("SHF", terms)
        self.assertNotIn("CFX", terms)
        self.assertIsNone(resolver.resolve("SHF"))
        self.assertIsNone(resolver.resolve("CFX"))

    def test_extract_probe_terms_ignores_financial_metric_acronyms(self) -> None:
        terms = extract_probe_terms("Watch PE, PB, ROE, TAM, FCF, EBITDA and margin trend for 300750.SZ.")

        self.assertIn("300750.SZ", terms)
        for metric in ["PE", "PB", "ROE", "TAM", "FCF", "EBITDA"]:
            self.assertNotIn(metric, terms)

    def test_extract_probe_terms_ignores_common_research_words(self) -> None:
        terms = extract_probe_terms(
            "portfolio long 300750.SZ and 600519.SH. Watch margin trend, orders, revenue, "
            "cash flow, valuation sentiment, channel inventory, and thesis quality. AAPL, OPEN, CASH.US, ES=F."
        )

        self.assertIn("300750.SZ", terms)
        self.assertIn("600519.SH", terms)
        self.assertIn("AAPL", terms)
        self.assertIn("OPEN", terms)
        self.assertIn("CASH.US", terms)
        self.assertIn("ES=F", terms)
        for word in [
            "SZ",
            "SH",
            "Watch",
            "margin",
            "trend",
            "orders",
            "revenue",
            "cash",
            "flow",
            "valuation",
            "sentiment",
            "channel",
            "inventory",
            "thesis",
            "quality",
        ]:
            self.assertNotIn(word, terms)

    def test_heuristic_china_future_contract_code_creates_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)

            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Futures Desk",
                "CU2601.SHF long, watch inventory and close if price breaks support.",
            )

            self.assertEqual(result.resolved_symbols, ["CU2601.SHF"])
            self.assertEqual(len(result.project_ids), 1)
            project = repo.list_project_rows()[0]
            self.assertEqual(project["symbols"], "CU2601.SHF")
            instrument = repo.get_instrument("CU2601.SHF")
            self.assertEqual(instrument.market, Market.CN_FUT)
            self.assertTrue(instrument.metadata["synthetic"])

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
            self.assertEqual(rows[0]["title"], "腾讯控股 做多跟踪")
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

    def test_structured_partial_overlap_signal_updates_and_creates_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            ingestor = SignalIngestor(repo, InstrumentResolver(repo.list_instruments()))

            existing = ingestor.ingest(
                "Structured Desk",
                "300750.SZ long, watch battery margin.",
            )
            mixed = ingestor.ingest(
                "Structured Desk",
                "raw structured note",
                extraction=ExtractedInput(
                    signals=[
                        ExtractedSignal(
                            instruments=["300750.SZ", "600519.SH"],
                            direction="long",
                            source_logic="300750.SZ update and 600519.SH new long, track margin and demand.",
                            observation_logic="Exit if margin or demand breaks.",
                            logic_score=7,
                        )
                    ],
                ),
            )

            rows = repo.list_project_rows()
            self.assertEqual(mixed.input_action, "mixed")
            self.assertEqual(len(mixed.project_ids), 2)
            self.assertIn(existing.project_ids[0], mixed.project_ids)
            self.assertEqual(len(rows), 2)
            self.assertEqual(mixed.resolved_symbols, ["300750.SZ", "600519.SH"])
            new_project_id = next(project_id for project_id in mixed.project_ids if project_id != existing.project_ids[0])
            new_legs = repo.list_project_legs(new_project_id)
            self.assertEqual([leg["symbol"] for leg in new_legs], ["600519.SH"])
            existing_logic_types = [block["logic_type"] for block in repo.list_logic_blocks(existing.project_ids[0])]
            self.assertIn("source_update", existing_logic_types)

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
            with db.session() as conn:
                conn.execute(
                    "UPDATE tracking_projects SET entry_date = ? WHERE id = ?",
                    (date(2026, 6, 6).isoformat(), opened.project_ids[0]),
                )
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
            html = render_dashboard(repo)
            self.assertIn("价格窗口（含平仓后一个月", html)
            self.assertIn("2026-05-06 至 2026-07-06", html)
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

    def test_cli_ingest_auto_publishes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
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
            self.assertIn("network exploded", payload["error"])
            self.assertIn("network exploded", payload["response_body"])
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

    def test_cli_daily_run_archives_reports_before_rendering_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            html_path = Path(tmp) / "dashboard.html"
            reports_dir = Path(tmp) / "reports"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(StringIO()):
                    cli_main(["ingest", "--source", "Daily Report Desk", "--text", "00700.HK long, watch ads recovery."])
                repo = Repository(Database(db_path))
                project_id = int(repo.list_project_rows()[0]["id"])
                repo.update_project_status(project_id, "exit_signal", needs_review=True)
                output = StringIO()
                with redirect_stdout(output):
                    code = cli_main([
                        "daily-run",
                        "--provider",
                        "fixture",
                        "--out",
                        str(html_path),
                        "--archive-reports",
                        "--reports-dir",
                        str(reports_dir),
                    ])

            payload = json.loads(output.getvalue())
            repo = Repository(Database(db_path))
            projects = repo.list_project_rows()
            report_path = reports_dir / f"project-{project_id}-report.md"
            reports = repo.list_project_reports(project_id=project_id)
            html = html_path.read_text(encoding="utf-8")
            self.assertEqual(code, 0)
            self.assertEqual(payload["checked_projects"], 1)
            self.assertEqual(payload["exit_signal_count"], 1)
            self.assertEqual(payload["exit_signals"][0]["id"], project_id)
            self.assertEqual(payload["exit_signals"][0]["action"], "exit_signal")
            self.assertEqual(len(payload["report_artifacts"]), 1)
            self.assertEqual(payload["report_artifacts"][0]["path"], str(report_path))
            self.assertTrue(report_path.exists())
            self.assertEqual(len(reports), 1)
            self.assertIn("已归档报告：", html)
            self.assertIn(f"project-{project_id}-report.md", html)

    def test_cli_check_auto_publishes_when_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            db = Database(db_path)
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "CLI Check Desk",
                "00700.HK long, watch ads recovery.",
            )
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with patch("signal_track.cli.DemoPublisher", FakeDemoPublisher):
                    with redirect_stdout(output):
                        code = cli_main(["check", "--provider", "fixture"])

            payload = json.loads(output.getvalue())
            events = Repository(Database(db_path)).list_publish_events()
            metadata = json.loads(events[0]["metadata"])
            self.assertEqual(code, 0)
            self.assertEqual(payload["checked_projects"], 1)
            self.assertTrue(payload["published"])
            self.assertEqual(payload["published_url"], "https://example.com/demo/signal")
            self.assertEqual(metadata["flow"], "check")

    def test_cli_no_publish_disables_auto_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "true",
                "GO_SITES_DEMO_PUBLISH_URL": "https://example.com/api/publish",
                "GO_SITES_DEMO_API_KEY": "demo-key",
                "TUSHARE_TOKEN": "",
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

    def test_cli_ingest_accepts_codex_structured_extraction_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            extraction_path = Path(tmp) / "codex-extraction.json"
            reports_dir = Path(tmp) / "reports"
            extraction_path.write_text(
                json.dumps(
                    {
                        "source_name": "Codex Desk",
                        "needs_review": False,
                        "notes": "Codex structured extraction",
                        "signals": [
                            {
                                "instruments": ["00700.HK"],
                                "action": "open",
                                "direction": "long",
                                "source_logic": "Tencent ad recovery and game revenue improvement.",
                                "observation_logic": "Review exit if ads recovery misses or price breaks below MA20.",
                                "logic_score": 8,
                                "is_portfolio": False,
                                "weights": {},
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(output):
                    code = cli_main([
                        "ingest",
                        "--source",
                        "Codex Desk",
                        "--text",
                        "raw source note",
                        "--extraction-json",
                        str(extraction_path),
                        "--archive-reports",
                        "--reports-dir",
                        str(reports_dir),
                    ])

            payload = json.loads(output.getvalue())
            repo = Repository(Database(db_path))
            projects = repo.list_project_rows()
            logic_blocks = repo.list_logic_blocks(int(projects[0]["id"]))
            reports = repo.list_project_reports(project_id=int(projects[0]["id"]))
            report_path = reports_dir / f"project-{int(projects[0]['id'])}-report.md"
            self.assertEqual(code, 0)
            self.assertEqual(payload["resolved_symbols"], ["00700.HK"])
            self.assertEqual(payload["input_action"], "track")
            self.assertEqual(payload["logic_score"], 8)
            self.assertEqual(len(payload["report_artifacts"]), 1)
            self.assertEqual(payload["report_artifacts"][0]["path"], str(report_path))
            self.assertTrue(report_path.exists())
            self.assertEqual(len(reports), 1)
            self.assertEqual(projects[0]["direction"], "long")
            self.assertTrue(any("Tencent ad recovery" in block["content"] for block in logic_blocks))
            self.assertTrue(any("MA20" in block["content"] for block in logic_blocks))

    def test_cli_list_projects_includes_performance_and_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
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
        self.assertEqual(project["latest_check"]["conclusion"], "needs_review")
        self.assertIn("Project logic score", project["latest_check"]["triggered_rules"])
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
                reports_output = StringIO()
                with redirect_stdout(reports_output):
                    reports_code = cli_main(["list-project-reports", "--project-id", str(project_id)])
                dashboard_html = render_dashboard(Repository(Database(db_path)))
                report_exists = report_path.exists()
                report_content = report_path.read_text(encoding="utf-8") if report_exists else ""

        payload = json.loads(json_output.getvalue())
        markdown_payload = json.loads(markdown_output.getvalue())
        reports_payload = json.loads(reports_output.getvalue())
        self.assertEqual(markdown_code, 0)
        self.assertEqual(json_code, 0)
        self.assertEqual(reports_code, 0)
        self.assertTrue(report_exists)
        self.assertIn("3C-5M-3D-3T", report_content)
        self.assertEqual(markdown_payload["path"], str(report_path))
        self.assertEqual(markdown_payload["report_artifact"]["format"], "markdown")
        self.assertEqual(markdown_payload["report_artifact"]["path"], str(report_path))
        self.assertEqual(markdown_payload["report_artifact"]["size_bytes"], len(report_content.encode("utf-8")))
        self.assertEqual(len(markdown_payload["report_artifact"]["content_hash"]), 64)
        self.assertEqual(len(reports_payload["reports"]), 1)
        self.assertEqual(reports_payload["reports"][0]["id"], markdown_payload["report_artifact_id"])
        self.assertIn("已归档报告：", dashboard_html)
        self.assertIn("project-report.md", dashboard_html)
        self.assertEqual(payload["project"]["source_name"], "CLI Report Desk")
        self.assertEqual(payload["instruments"][0]["symbol"], "00700.HK")

    def test_cli_update_project_weights(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
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

    def test_cli_add_project_note_check_runs_without_backend_evaluator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
            }
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(StringIO()):
                    cli_main([
                        "ingest",
                        "--source",
                        "CLI Note Desk",
                        "--text",
                        "00700.HK long, watch ads.",
                    ])
                repo = Repository(Database(db_path))
                project_id = int(repo.list_project_rows()[0]["id"])
                output = StringIO()
                with redirect_stdout(output):
                    code = cli_main([
                        "add-project-note",
                        str(project_id),
                        "--text",
                        "manual observation: ads data improved",
                        "--check",
                        "--provider",
                        "fixture",
                    ])

            payload = json.loads(output.getvalue())
            checks = Repository(Database(db_path)).list_daily_checks(project_id=project_id)
            self.assertEqual(code, 0)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["checked_projects"], 1)
            self.assertTrue(checks)

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
            self.assertEqual(check["conclusion"], "needs_review")
            self.assertIn("Project logic score", check["triggered_rules"])
            self.assertEqual(project["status"], "needs_review")
            self.assertTrue(bool(project["needs_review"]))

    def test_daily_check_surfaces_portfolio_weight_review_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            opened = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                "Portfolio Desk",
                "portfolio long: 300750.SZ, 600519.SH, watch margin and demand.",
            )
            project_id = opened.project_ids[0]

            DailyChecker(repo, FixtureMarketDataProvider()).run(next_fixture_trading_day(date.today()))

            project = repo.get_project_row(project_id)
            check = repo.list_daily_checks(project_id=project_id)[0]
            self.assertEqual(check["conclusion"], "needs_review")
            self.assertTrue(bool(project["weight_needs_review"]))
            self.assertIn("Portfolio weights need review", check["triggered_rules"])

    def test_daily_check_keeps_unresolved_instrument_project_in_review(self) -> None:
        class HoldEvaluator(DailyLogicEvaluator):
            def evaluate(self, *, project, logic_blocks, research_items, performance, previous_checks, check_date):
                del project, logic_blocks, research_items, performance, previous_checks, check_date
                return DailyEvaluation(
                    conclusion="hold",
                    summary="model would hold if the instrument were known",
                    triggered_rules=[],
                    confidence=0.5,
                )

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            source_id = repo.get_or_create_source("Unresolved Desk")
            project_id = repo.create_tracking_project(
                title="Unresolved target",
                source_id=source_id,
                raw_input_id=None,
                status="needs_review",
                direction="long",
                entry_date="2026-06-01",
                logic_score=8,
                needs_review=True,
                metadata={"raw_extract_status": "no_instrument_resolved"},
            )

            checked = DailyChecker(repo, evaluator=HoldEvaluator()).run(date(2026, 6, 5))

            project = repo.get_project_row(project_id)
            check = repo.list_daily_checks(project_id=project_id)[0]
            self.assertEqual(checked, 1)
            self.assertEqual(project["status"], "needs_review")
            self.assertTrue(bool(project["needs_review"]))
            self.assertEqual(check["conclusion"], "needs_review")
            self.assertIn("No resolved instrument", json.loads(check["triggered_rules"])[0])

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

    def test_daily_check_preserves_existing_exit_signal_until_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Exit Desk",
                content="00700.HK long, watch ads recovery.",
            )
            first_check = next_fixture_trading_day(date.today())
            second_check = next_fixture_trading_day(first_check)

            DailyChecker(repo, FixtureMarketDataProvider(), evaluator=FakeDailyEvaluator()).run(first_check)
            DailyChecker(repo, FixtureMarketDataProvider()).run(second_check)

            project = repo.get_project_row(result.project_ids[0])
            checks = repo.list_daily_checks(project_id=result.project_ids[0], limit=2)
            self.assertEqual(project["status"], "exit_signal")
            self.assertEqual(checks[0]["check_date"], second_check.isoformat())
            self.assertEqual(checks[0]["conclusion"], "exit_signal")
            self.assertIn("Existing exit signal remains open", checks[0]["triggered_rules"])

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

    def test_daily_check_triggers_moving_average_break_above_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "signal_track.sqlite3")
            db.init()
            repo = Repository(db)
            for instrument in SEED_INSTRUMENTS:
                repo.upsert_instrument(instrument)
            result = SignalIngestor(repo, InstrumentResolver(repo.list_instruments())).ingest(
                source_name="Short Source",
                content="NVDA short. Exit if price breaks above 5 day moving average.",
            )
            instrument = repo.get_instrument("NVDA")
            self.assertIsNotNone(instrument)
            instrument_id = repo.upsert_instrument(instrument)
            closes = [100, 100, 100, 100, 120]
            bars = [
                DailyBar(
                    symbol="NVDA",
                    provider_symbol="NVDA",
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
            self.assertIn("突破 5 日均线", checks[0]["triggered_rules"])

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
        leading_loss_hits = evaluate_return_rules("8% stop-loss; 10% drawdown", loss_performance)
        leading_profit_hits = evaluate_return_rules("15% take profit; 20% upside", profit_performance)
        chinese_thresholds = extract_percent_thresholds("8%止损；10%回撤止损", ("止损", "回撤"))

        self.assertEqual([hit.rule_type for hit in loss_hits], ["return_drawdown"])
        self.assertEqual([hit.rule_type for hit in profit_hits], ["return_take_profit"])
        self.assertEqual([hit.rule_type for hit in leading_loss_hits], ["return_drawdown"])
        self.assertEqual([hit.rule_type for hit in leading_profit_hits], ["return_take_profit"])
        self.assertEqual(chinese_thresholds, [0.08, 0.1])

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

    def test_cli_file_ingest_accepts_utf8_chinese_note_and_archives_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signal_track.sqlite3"
            note_path = Path(tmp) / "source-note.md"
            reports_dir = Path(tmp) / "reports"
            note_path.write_text(
                "00700.HK 做多，观察广告和游戏恢复。如果跌破5日线则平仓复核。",
                encoding="utf-8",
            )
            env = {
                "SIGNAL_TRACK_DB_PATH": str(db_path),
                "SIGNAL_TRACK_AUTO_PUBLISH_ON_UPDATE": "false",
                "GO_SITES_DEMO_PUBLISH_URL": "",
                "GO_SITES_DEMO_API_KEY": "",
                "TUSHARE_TOKEN": "",
            }
            output = StringIO()
            with patch.dict("os.environ", env, clear=False):
                with redirect_stdout(output):
                    code = cli_main([
                        "ingest",
                        "--source",
                        "中文文件源",
                        "--file",
                        str(note_path),
                        "--archive-reports",
                        "--reports-dir",
                        str(reports_dir),
                    ])

            payload = json.loads(output.getvalue())
            repo = Repository(Database(db_path))
            projects = repo.list_project_rows()
            project_id = int(projects[0]["id"])
            report_path = reports_dir / f"project-{project_id}-report.md"
            self.assertEqual(code, 0)
            self.assertEqual(payload["input_action"], "track")
            self.assertEqual(payload["resolved_symbols"], ["00700.HK"])
            self.assertEqual(payload["projects"][0]["direction"], "long")
            self.assertEqual(projects[0]["source_name"], "中文文件源")
            self.assertIn("腾讯控股", projects[0]["title"])
            self.assertTrue(report_path.exists())
            self.assertEqual(payload["report_artifacts"][0]["path"], str(report_path))


if __name__ == "__main__":
    unittest.main()
