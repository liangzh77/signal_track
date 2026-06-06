from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from collections.abc import Iterable

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
        self._routes: dict[Market, list[MarketDataProvider]] = {}
        for route in routes:
            providers = self._routes.setdefault(route.market, [])
            if route.provider not in providers:
                providers.append(route.provider)

    @classmethod
    def from_market_map(
        cls,
        routes: dict[Market, MarketDataProvider | Iterable[MarketDataProvider]],
    ) -> "AutoMarketDataProvider":
        provider_routes: list[ProviderRoute] = []
        for market, providers in routes.items():
            if isinstance(providers, MarketDataProvider):
                provider_routes.append(ProviderRoute(market, providers))
                continue
            provider_routes.extend(ProviderRoute(market, provider) for provider in providers)
        return cls(provider_routes)

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        providers = self._providers_for(instrument.market)
        errors: list[str] = []
        empty_providers: list[str] = []
        for provider in providers:
            try:
                bars = provider.get_daily_bars(instrument, start_date, end_date, adjustment)
            except Exception as exc:
                errors.append(f"{provider.name}: {exc}")
                continue
            if bars:
                return bars
            empty_providers.append(provider.name)
        if empty_providers and not errors:
            return []
        errors.extend(f"{provider_name}: no bars returned" for provider_name in empty_providers)
        detail = "; ".join(errors) if errors else "no configured provider"
        raise ValueError(f"Auto provider failed for {instrument.market.value}/{instrument.symbol}: {detail}")

    def list_instruments(self, market: Market) -> list[Instrument]:
        providers = self._routes.get(market, [])
        for provider in providers:
            try:
                return provider.list_instruments(market)
            except NotImplementedError:
                continue
        return [instrument for instrument in SEED_INSTRUMENTS if instrument.market == market]

    def _providers_for(self, market: Market) -> list[MarketDataProvider]:
        providers = self._routes.get(market, [])
        if not providers:
            raise ValueError(f"Auto provider has no route for {market.value}")
        return providers
