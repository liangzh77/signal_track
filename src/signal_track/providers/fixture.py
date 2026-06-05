from __future__ import annotations

from datetime import date, timedelta

from signal_track.models import DailyBar, Instrument
from signal_track.providers.base import MarketDataProvider


class FixtureMarketDataProvider(MarketDataProvider):
    name = "fixture"

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        del adjustment
        bars: list[DailyBar] = []
        current = start_date
        base = 40 + stable_seed(instrument.symbol) % 300
        i = 0
        while current <= end_date:
            if current.weekday() < 5:
                drift = i * 0.35
                wave = ((i % 9) - 4) * 0.18
                close = round(base + drift + wave, 2)
                open_price = round(close - 0.4, 2)
                high = round(close + 1.2, 2)
                low = round(open_price - 1.1, 2)
                bars.append(
                    DailyBar(
                        symbol=instrument.symbol,
                        provider_symbol=instrument.provider_symbol,
                        date=current,
                        open=open_price,
                        high=high,
                        low=low,
                        close=close,
                        adj_close=close,
                        volume=float(1_000_000 + i * 10_000),
                        amount=float(close * (1_000_000 + i * 10_000)),
                        settle=close if "FUT" in instrument.market.value else None,
                        open_interest=float(10_000 + i * 25) if "FUT" in instrument.market.value else None,
                        provider=self.name,
                    )
                )
                i += 1
            current += timedelta(days=1)
        return bars


def stable_seed(text: str) -> int:
    return sum((index + 1) * ord(char) for index, char in enumerate(text))

