from __future__ import annotations

from datetime import date, timedelta
import math
import re

from signal_track.models import AssetType, DailyBar, Instrument, Market
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
        if instrument.market not in {Market.US, Market.US_FUT, Market.HK, Market.HK_FUT}:
            raise ValueError(f"yfinance provider does not support {instrument.market}")

        auto_adjust = adjustment in {"adj", "auto"}
        provider_symbol = yfinance_symbol(instrument)
        frame = self.yf.download(
            provider_symbol,
            start=start_date.isoformat(),
            end=(end_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=auto_adjust,
            progress=False,
        )
        bars: list[DailyBar] = []
        for index, row in frame.iterrows():
            bar_date = index.date()
            close = get_price_field(row, provider_symbol, "Close")
            bars.append(
                DailyBar(
                    symbol=instrument.symbol,
                    provider_symbol=provider_symbol,
                    date=bar_date,
                    open=get_price_field(row, provider_symbol, "Open"),
                    high=get_price_field(row, provider_symbol, "High"),
                    low=get_price_field(row, provider_symbol, "Low"),
                    close=close,
                    adj_close=get_price_field(row, provider_symbol, "Adj Close") or close,
                    volume=get_price_field(row, provider_symbol, "Volume"),
                    provider=self.name,
                )
            )
        return bars


def yfinance_symbol(instrument: Instrument) -> str:
    symbol = instrument.provider_symbol
    if instrument.market == Market.HK and instrument.asset_type in {AssetType.STOCK, AssetType.ETF, AssetType.INDEX}:
        match = re.fullmatch(r"(\d{1,5})\.HK", symbol.upper())
        if match:
            return f"{match.group(1)[-4:].zfill(4)}.HK"
    return symbol


def get_price_field(row: object, provider_symbol: str, field: str) -> float | None:
    candidates = [
        field,
        (field, provider_symbol),
        (provider_symbol, field),
    ]
    for key in candidates:
        value = get_row_value(row, key)
        parsed = to_optional_float(value)
        if parsed is not None:
            return parsed
    return None


def get_row_value(row: object, key: object) -> object | None:
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        pass
    getter = getattr(row, "get", None)
    if getter is None:
        return None
    try:
        return getter(key)
    except (KeyError, TypeError, IndexError):
        return None


def to_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if hasattr(value, "iloc"):
        try:
            value = value.iloc[0]  # type: ignore[attr-defined]
        except (IndexError, TypeError, ValueError):
            return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
