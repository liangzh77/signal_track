from __future__ import annotations

from signal_track.config import Settings
from signal_track.models import Market
from signal_track.providers.auto import AutoMarketDataProvider
from signal_track.providers.base import MarketDataProvider
from signal_track.providers.fixture import FixtureMarketDataProvider
from signal_track.providers.tushare_provider import TushareMarketDataProvider
from signal_track.providers.yfinance_provider import YFinanceMarketDataProvider


def build_market_data_provider(name: str, settings: Settings) -> MarketDataProvider | None:
    if name == "none":
        return None
    if name == "fixture":
        return FixtureMarketDataProvider()
    if name == "auto":
        return build_auto_provider(settings)
    if name == "tushare":
        if not settings.tushare_token:
            raise ValueError("TUSHARE_TOKEN is required for the tushare provider")
        try:
            return TushareMarketDataProvider(settings.tushare_token)
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    if name == "yfinance":
        try:
            return YFinanceMarketDataProvider()
        except RuntimeError as exc:
            raise ValueError(str(exc)) from exc
    raise ValueError(f"Unknown market data provider: {name}")


def build_auto_provider(settings: Settings) -> AutoMarketDataProvider:
    routes: dict[Market, MarketDataProvider] = {}
    errors: list[str] = []

    if settings.tushare_token:
        try:
            tushare = TushareMarketDataProvider(settings.tushare_token)
        except RuntimeError as exc:
            errors.append(str(exc))
        else:
            for market in (Market.CN_A, Market.HK, Market.CN_FUT, Market.US):
                routes[market] = tushare

    try:
        yfinance = YFinanceMarketDataProvider()
    except RuntimeError as exc:
        errors.append(str(exc))
    else:
        for market in (Market.HK, Market.HK_FUT, Market.US, Market.US_FUT):
            routes.setdefault(market, yfinance)

    if not routes:
        detail = "; ".join(errors) if errors else "no market providers configured"
        raise ValueError(f"Auto provider requires TUSHARE_TOKEN and/or installed yfinance: {detail}")

    return AutoMarketDataProvider.from_market_map(routes)
