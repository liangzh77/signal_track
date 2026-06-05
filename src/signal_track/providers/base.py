from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from signal_track.models import DailyBar, Instrument


class MarketDataProvider(ABC):
    name: str

    @abstractmethod
    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        """Return normalized daily bars for an instrument."""

