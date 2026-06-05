from __future__ import annotations

from dataclasses import dataclass

from .db import Repository
from .models import Instrument, Market
from .providers.base import MarketDataProvider


@dataclass(frozen=True)
class RefreshResult:
    market: Market
    count: int
    symbols: list[str]


class InstrumentMasterService:
    def __init__(self, repo: Repository, provider: MarketDataProvider):
        self.repo = repo
        self.provider = provider

    def refresh(self, market: Market) -> RefreshResult:
        instruments = self.provider.list_instruments(market)
        symbols = []
        for instrument in instruments:
            self.repo.upsert_instrument(instrument)
            symbols.append(instrument.symbol)
        return RefreshResult(market=market, count=len(symbols), symbols=symbols)

    def refresh_many(self, markets: list[Market]) -> list[RefreshResult]:
        return [self.refresh(market) for market in markets]

