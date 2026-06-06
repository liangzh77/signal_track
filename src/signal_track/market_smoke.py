from __future__ import annotations

from datetime import date, timedelta

from .db import Repository
from .models import Instrument, Market
from .providers.base import MarketDataProvider
from .resolver import SEED_INSTRUMENTS


def market_data_smoke(
    repo: Repository,
    provider: MarketDataProvider,
    markets: list[Market] | None = None,
    days: int = 30,
    sample_size: int = 1,
    end_date: date | None = None,
) -> dict:
    end = end_date or date.today()
    start = end - timedelta(days=max(days, 1))
    selected_markets = markets or all_markets()
    rows = []
    for market in selected_markets:
        samples = sample_instruments(repo, market, sample_size)
        if not samples:
            rows.append(
                {
                    "market": market.value,
                    "symbol": None,
                    "provider_symbol": None,
                    "ok": False,
                    "bar_count": 0,
                    "latest_date": None,
                    "error": "no sample instrument available",
                }
            )
            continue
        for instrument in samples:
            rows.append(smoke_instrument(provider, instrument, start, end))

    return {
        "ok": bool(rows) and all(row["ok"] for row in rows),
        "provider": provider.name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "sample_size": sample_size,
        "markets": rows,
    }


def smoke_instrument(
    provider: MarketDataProvider,
    instrument: Instrument,
    start: date,
    end: date,
) -> dict:
    try:
        bars = provider.get_daily_bars(instrument, start, end)
    except Exception as exc:
        return {
            "market": instrument.market.value,
            "symbol": instrument.symbol,
            "provider_symbol": instrument.provider_symbol,
            "ok": False,
            "bar_count": 0,
            "latest_date": None,
            "error": str(exc),
        }

    latest_date = max((bar.date for bar in bars), default=None)
    has_close = any(bar.close is not None for bar in bars)
    return {
        "market": instrument.market.value,
        "symbol": instrument.symbol,
        "provider_symbol": instrument.provider_symbol,
        "ok": bool(bars) and has_close,
        "bar_count": len(bars),
        "latest_date": latest_date.isoformat() if latest_date else None,
        "error": None if bars and has_close else "no bars with close price returned",
    }


def sample_instruments(repo: Repository, market: Market, sample_size: int) -> list[Instrument]:
    size = max(sample_size, 1)
    seed_samples = [
        instrument
        for instrument in SEED_INSTRUMENTS
        if instrument.market == market
    ]
    seed_symbols = {instrument.symbol for instrument in seed_samples}
    repo_samples = [
        instrument
        for instrument in repo.list_instruments()
        if instrument.market == market and instrument.symbol not in seed_symbols
    ]
    return [*seed_samples, *repo_samples][:size]


def all_markets() -> list[Market]:
    return [Market.CN_A, Market.HK, Market.CN_FUT, Market.HK_FUT, Market.US, Market.US_FUT]
