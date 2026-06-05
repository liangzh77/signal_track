from __future__ import annotations

from datetime import date
from typing import Any

from signal_track.models import AssetType, DailyBar, Instrument, Market
from signal_track.providers.base import MarketDataProvider


class TushareMarketDataProvider(MarketDataProvider):
    name = "tushare"

    def __init__(self, token: str):
        if not token:
            raise ValueError("Tushare token is required")
        try:
            import tushare as ts  # type: ignore
        except ImportError as exc:
            raise RuntimeError("Install market extras first: pip install -e .[market]") from exc
        ts.set_token(token)
        self.pro = ts.pro_api()

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        del adjustment
        start = start_date.strftime("%Y%m%d")
        end = end_date.strftime("%Y%m%d")

        if instrument.market == Market.CN_A:
            frame = self.pro.daily(ts_code=instrument.provider_symbol, start_date=start, end_date=end)
            return [self._from_stock_row(instrument, row) for row in frame.to_dict("records")]

        if instrument.market == Market.HK:
            frame = self.pro.hk_daily(ts_code=instrument.provider_symbol, start_date=start, end_date=end)
            return [self._from_stock_row(instrument, row) for row in frame.to_dict("records")]

        if instrument.market == Market.US:
            frame = self.pro.us_daily(ts_code=instrument.provider_symbol, start_date=start, end_date=end)
            return [self._from_stock_row(instrument, row) for row in frame.to_dict("records")]

        if instrument.market == Market.CN_FUT:
            if instrument.asset_type == AssetType.CONTINUOUS_FUTURE:
                return self._continuous_future_bars(instrument, start, end)
            frame = self.pro.fut_daily(ts_code=instrument.provider_symbol, start_date=start, end_date=end)
            return [self._from_future_row(instrument, row) for row in frame.to_dict("records")]

        raise ValueError(f"Tushare provider does not support {instrument.market}")

    def _continuous_future_bars(self, instrument: Instrument, start: str, end: str) -> list[DailyBar]:
        mapping = self.pro.fut_mapping(ts_code=instrument.provider_symbol, start_date=start, end_date=end)
        bars: list[DailyBar] = []
        for row in mapping.to_dict("records"):
            contract = row.get("mapping_ts_code") or row.get("mapping_ts_code")
            trade_date = row.get("trade_date")
            if not contract or not trade_date:
                continue
            frame = self.pro.fut_daily(ts_code=contract, start_date=trade_date, end_date=trade_date)
            for bar_row in frame.to_dict("records"):
                bars.append(self._from_future_row(instrument, bar_row, provider_symbol=contract))
        return sorted(bars, key=lambda bar: bar.date)

    def _from_stock_row(self, instrument: Instrument, row: dict[str, Any]) -> DailyBar:
        return DailyBar(
            symbol=instrument.symbol,
            provider_symbol=row.get("ts_code") or instrument.provider_symbol,
            date=parse_tushare_date(row["trade_date"]),
            open=to_float(row.get("open")),
            high=to_float(row.get("high")),
            low=to_float(row.get("low")),
            close=to_float(row.get("close")),
            adj_close=to_float(row.get("close")),
            volume=to_float(row.get("vol") or row.get("volume")),
            amount=to_float(row.get("amount")),
            provider=self.name,
        )

    def _from_future_row(
        self,
        instrument: Instrument,
        row: dict[str, Any],
        provider_symbol: str | None = None,
    ) -> DailyBar:
        return DailyBar(
            symbol=instrument.symbol,
            provider_symbol=provider_symbol or row.get("ts_code") or instrument.provider_symbol,
            date=parse_tushare_date(row["trade_date"]),
            open=to_float(row.get("open")),
            high=to_float(row.get("high")),
            low=to_float(row.get("low")),
            close=to_float(row.get("close")),
            adj_close=to_float(row.get("close")),
            volume=to_float(row.get("vol")),
            amount=to_float(row.get("amount")),
            settle=to_float(row.get("settle")),
            open_interest=to_float(row.get("oi")),
            provider=self.name,
        )


def parse_tushare_date(value: str) -> date:
    return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(value)

