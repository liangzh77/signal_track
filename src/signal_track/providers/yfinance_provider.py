from __future__ import annotations

from datetime import date, timedelta

from signal_track.models import DailyBar, Instrument, Market
from signal_track.providers.base import MarketDataProvider


class YFinanceMarketDataProvider(MarketDataProvider):
    name = "yfinance"

    def __init__(self) -> None:
        try:
            import yfinance as yf  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install market extras first: pip install -e .[market]") from exc
        self.yf = yf

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        if instrument.market not in {Market.US, Market.US_FUT, Market.HK}:
            raise ValueError(f"yfinance provider does not support {instrument.market}")

        auto_adjust = adjustment in {"adj", "auto"}
        frame = self.yf.download(
            instrument.provider_symbol,
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=auto_adjust,
            progress=False,
        )
        bars: list[DailyBar] = []
        for index, row in frame.iterrows():
            bar_date = index.date()
            close = to_optional_float(row.get("Close"))
            bars.append(
                DailyBar(
                    symbol=instrument.symbol,
                    provider_symbol=instrument.provider_symbol,
                    date=bar_date,
                    open=to_optional_float(row.get("Open")),
                    high=to_optional_float(row.get("High")),
                    low=to_optional_float(row.get("Low")),
                    close=close,
                    adj_close=to_optional_float(row.get("Adj Close")) or close,
                    volume=to_optional_float(row.get("Volume")),
                    provider=self.name,
                )
            )
        return bars


def to_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

