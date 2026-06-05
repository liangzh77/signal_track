from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import date
from pathlib import Path

from signal_track.db import Database, Repository
from signal_track.checker import DailyChecker
from signal_track.dashboard import render_dashboard
from signal_track.extraction import ExtractedInput, ExtractedSignal
from signal_track.instrument_master import InstrumentMasterService
from signal_track.market_data import MarketDataService
from signal_track.models import DailyBar, Market
from signal_track.publisher import extract_published_address
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.resolver import InstrumentResolver, SEED_INSTRUMENTS
from signal_track.signals import SignalIngestor

try:
    from fastapi.testclient import TestClient
    from signal_track.web_app import create_app
except Exception:
    TestClient = None
    create_app = None


class SignalTrackCoreTests(unittest.TestCase):
    def test_resolves_seed_instruments_across_markets(self) -> None:
        resolver = InstrumentResolver()

        cases = [
            ("宁德时代", Market.CN_A, "300750.SZ"),
            ("00700", Market.HK, "00700.HK"),
            ("铜主连", Market.CN_FUT, "CU.SHF"),
            ("NVDA.US", Market.US, "NVDA"),
            ("纳指期货", Market.US_FUT, "NQ"),
        ]

        for query, market, expected in cases:
            with self.subTest(query=query):
                resolution = resolver.resolve(query, market)
                self.assertIsNotNone(resolution)
                self.assertEqual(resolution.instrument.symbol, expected)
                self.assertGreaterEqual(resolution.confidence, 0.6)

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
            self.assertEqual(version, 1)
            with db.session() as conn:
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(tracking_projects)")}
            self.assertIn("logic_score", columns)
            self.assertIn("weight_needs_review", columns)

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

            checked = DailyChecker(repo, FixtureMarketDataProvider()).run(date(2026, 6, 5))
            self.assertEqual(checked, 1)

            html = render_dashboard(repo)
            self.assertIn("Signal Track 投资信号看板", html)
            self.assertIn("腾讯控股", html)
            self.assertIn("needs_review", html)
            self.assertIn("polyline", html)
            self.assertIn("系统补充逻辑", html)

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

    def test_extract_published_address(self) -> None:
        body = '{"address":"https://example.com/demo/a","title":"x"}'
        self.assertEqual(extract_published_address(body), "https://example.com/demo/a")
        self.assertIsNone(extract_published_address("not json"))

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

            allowed = client.post(
                "/api/inputs",
                headers={"X-Signal-Track-Key": "secret-key"},
                json={"source": "测试源", "content": "腾讯 做多"},
            )
            self.assertEqual(allowed.status_code, 200)
            self.assertEqual(allowed.json()["resolved_symbols"], ["00700.HK"])

            health = client.get("/health")
            self.assertEqual(health.status_code, 200)


if __name__ == "__main__":
    unittest.main()
