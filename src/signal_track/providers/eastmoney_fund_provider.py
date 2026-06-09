from __future__ import annotations

import json
import math
from datetime import date
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from signal_track.models import DailyBar, Instrument
from signal_track.providers.base import MarketDataProvider


class EastmoneyFundProvider(MarketDataProvider):
    name = "eastmoney_fund"
    api_url = "https://api.fund.eastmoney.com/f10/lsjz"

    def get_daily_bars(
        self,
        instrument: Instrument,
        start_date: date,
        end_date: date,
        adjustment: str = "none",
    ) -> list[DailyBar]:
        del adjustment
        fund_code = fund_code_for(instrument)
        if not fund_code:
            raise ValueError(f"Eastmoney fund provider only supports fund codes: {instrument.symbol}")
        payload = self._fetch_nav_history(fund_code, start_date, end_date)
        return bars_from_payload(payload, instrument, fund_code, start_date, end_date)

    def _fetch_nav_history(self, fund_code: str, start_date: date, end_date: date) -> dict:
        rows = []
        total_count = None
        page_index = 1
        while True:
            payload = self._fetch_nav_page(fund_code, start_date, end_date, page_index)
            data = payload.get("Data") or {}
            page_rows = data.get("LSJZList") if isinstance(data, dict) else None
            if not isinstance(page_rows, list) or not page_rows:
                break
            rows.extend(page_rows)
            total_count = payload.get("TotalCount")
            if isinstance(total_count, int) and len(rows) >= total_count:
                break
            page_size = int(payload.get("PageSize") or len(page_rows) or 20)
            if len(page_rows) < page_size:
                break
            page_index += 1
        return {
            "Data": {"LSJZList": rows},
            "ErrCode": 0,
            "ErrMsg": None,
            "TotalCount": len(rows) if total_count is None else total_count,
            "PageIndex": 1,
            "PageSize": len(rows),
        }

    def _fetch_nav_page(self, fund_code: str, start_date: date, end_date: date, page_index: int) -> dict:
        query = urlencode(
            {
                "fundCode": fund_code,
                "pageIndex": page_index,
                "pageSize": max(20, (end_date - start_date).days + 10),
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
            }
        )
        request = Request(
            f"{self.api_url}?{query}",
            headers={
                "Referer": "https://fundf10.eastmoney.com/",
                "User-Agent": "Mozilla/5.0",
            },
        )
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))


def fund_code_for(instrument: Instrument) -> str | None:
    symbol = instrument.provider_symbol or instrument.symbol
    code = symbol.split(".", 1)[0]
    if code.isdigit() and len(code) == 6 and (
        instrument.symbol.upper().endswith(".OF")
        or instrument.provider_symbol.upper().endswith(".OF")
        or instrument.exchange.upper() == "OF"
        or instrument.metadata.get("fund_type") == "open_fund"
    ):
        return code
    return None


def bars_from_payload(
    payload: dict,
    instrument: Instrument,
    fund_code: str,
    start_date: date,
    end_date: date,
) -> list[DailyBar]:
    data = payload.get("Data") or {}
    rows = data.get("LSJZList", []) if isinstance(data, dict) else []
    if not isinstance(rows, list):
        return []
    bars: list[DailyBar] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bar_date = parse_date(row.get("FSRQ"))
        close = to_float(row.get("DWJZ"))
        cumulative_nav = to_float(row.get("LJJZ"))
        if bar_date is None or close is None or not (start_date <= bar_date <= end_date):
            continue
        bars.append(
            DailyBar(
                symbol=instrument.symbol,
                provider_symbol=fund_code,
                date=bar_date,
                open=close,
                high=close,
                low=close,
                close=close,
                adj_close=cumulative_nav or close,
                provider=EastmoneyFundProvider.name,
            )
        )
    return sorted(bars, key=lambda bar: bar.date)


def parse_date(value: object) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
