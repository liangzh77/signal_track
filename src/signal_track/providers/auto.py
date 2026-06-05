from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from signal_track.models import DailyBar, Instrument, Market
from signal_track.providers.base import MarketDataProvider
from signal_track.resolver import SEED_INSTRUMENTS


@dataclass(frozen=True)
class ProviderRoute:
    market: Market
    provider: MarketDataProvider


class AutoMarketDataProvider(MarketDataProvider):
    name = "auto"

    def __init__(self, routes: list[ProviderRoute]) -> None:
        self._routes: dict[Market, MarketDataProvider] = {}
        for route in routes:
            self._routes.setdefault(route.market, route.provider)

    @classmethod
    def from_market_map(cls, routes: dict[Market, MarketDataProvider]) -> "AutoMarketDataProvider":
        return cls([ProviderRoute(market, provider) for market, provider in routes.items()])

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        provider = self._provider_for(instrument.market)
        return provider.get_daily_bars(instrument, start_date, end_date, adjustment)

    def list_instruments(self, market: Market) -> list[Instrument]:
        provider = self._routes.get(market)
        if provider:
            try:
                return provider.list_instruments(market)
            except NotImplementedError:
                pass
        return [instrument for instrument in SEED_INSTRUMENTS if instrument.market == market]

    def _provider_for(self, market: Market) -> MarketDataProvider:
        provider = self._routes.get(market)
        if not provider:
            raise ValueError(f"Auto provider has no route for {market.value}")
        return provider
