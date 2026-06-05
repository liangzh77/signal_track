from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from signal_track.db import Database, Repository
from signal_track.checker import DailyChecker
from signal_track.dashboard import render_dashboard
from signal_track.extraction import ExtractedInput, ExtractedSignal
from signal_track.market_data import MarketDataService
from signal_track.models import Market
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.resolver import InstrumentResolver, SEED_INSTRUMENTS
from signal_track.signals import SignalIngestor


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


if __name__ == "__main__":
    unittest.main()
