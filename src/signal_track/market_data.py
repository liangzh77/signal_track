from __future__ import annotations

from datetime import date

from .db import Repository
from .models import DailyBar, Instrument
from .providers.base import MarketDataProvider


class MarketDataService:
    def __init__(self, repo: Repository, provider: MarketDataProvider):
        self.repo = repo
        self.provider = provider

    def fetch_and_store(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        instrument_id = self.repo.upsert_instrument(instrument)
        bars = self.provider.get_daily_bars(instrument, start_date, end_date, adjustment)
        self.repo.upsert_bars(instrument_id, bars)
        return bars

