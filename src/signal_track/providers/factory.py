from __future__ import annotations

from signal_track.config import Settings
from signal_track.providers.base import MarketDataProvider
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.providers.tushare_provider import TushareMarketDataProvider
from signal_track.providers.yfinance_provider import YFinanceMarketDataProvider


def build_market_data_provider(name: str, settings: Settings) -> MarketDataProvider | None:
    if name == "none":
        return None
    if name == "fixture":
        return FixtureMarketDataProvider()
    if name == "tushare":
        if not settings.tushare_token:
            raise ValueError("TUSHARE_TOKEN is required for the tushare provider")
        return TushareMarketDataProvider(settings.tushare_token)
    if name == "yfinance":
        return YFinanceMarketDataProvider()
    raise ValueError(f"Unknown market data provider: {name}")

