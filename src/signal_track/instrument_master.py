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
    skipped: bool = False
    error: str | None = None


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
        results: list[RefreshResult] = []
        for market in markets:
            try:
                results.append(self.refresh(market))
            except (NotImplementedError, ValueError) as exc:
                results.append(RefreshResult(market=market, count=0, symbols=[], skipped=True, error=str(exc)))
        return results
