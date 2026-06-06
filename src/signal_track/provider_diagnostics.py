from __future__ import annotations

from importlib.util import find_spec

from .config import Settings
from .models import Market


def market_data_coverage(settings: Settings, provider_name: str = "auto") -> dict:
    tushare_installed = dependency_available("tushare")
    yfinance_installed = dependency_available("yfinance")
    tushare_ready = bool(settings.tushare_token) and tushare_installed
    yfinance_ready = yfinance_installed

    if provider_name == "auto":
        rows = auto_coverage(tushare_ready, yfinance_ready)
    elif provider_name == "fixture":
        rows = [
            coverage_row(market, "fixture", "seed_fallback", False, ["representative local seed data"])
            for market in all_markets()
        ]
    elif provider_name == "tushare":
        rows = tushare_coverage(tushare_ready)
    elif provider_name == "yfinance":
        rows = yfinance_coverage(yfinance_ready)
    elif provider_name == "none":
        rows = [coverage_row(market, None, "seed_fallback", False, ["price refresh disabled"]) for market in all_markets()]
    else:
        raise ValueError(f"Unknown market data provider: {provider_name}")

    return {
        "provider": provider_name,
        "dependencies": {
            "tushare_token_configured": bool(settings.tushare_token),
            "tushare_installed": tushare_installed,
            "yfinance_installed": yfinance_installed,
        },
        "markets": rows,
    }


def auto_coverage(tushare_ready: bool, yfinance_ready: bool) -> list[dict]:
    rows: list[dict] = []
    for market in all_markets():
        notes: list[str] = []
        price_provider = None
        master_provider = "seed_fallback"
        real_master = False

        if market in {Market.CN_A, Market.HK, Market.CN_FUT, Market.US} and tushare_ready:
            price_provider = "tushare"
            master_provider = "tushare"
            real_master = True

        if market in {Market.HK, Market.HK_FUT, Market.US, Market.US_FUT} and price_provider is None and yfinance_ready:
            price_provider = "yfinance"

        if master_provider == "seed_fallback":
            notes.append("instrument master uses built-in seed fallback")
        if price_provider is None:
            notes.append(missing_dependency_note(market, tushare_ready, yfinance_ready))

        rows.append(coverage_row(market, price_provider, master_provider, real_master, notes))
    return rows


def tushare_coverage(tushare_ready: bool) -> list[dict]:
    rows: list[dict] = []
    for market in all_markets():
        supported = market in {Market.CN_A, Market.HK, Market.CN_FUT, Market.US}
        notes: list[str] = []
        if not supported:
            notes.append("tushare route is not configured for this market")
        if supported and not tushare_ready:
            notes.append("requires TUSHARE_TOKEN and installed tushare package")
        rows.append(
            coverage_row(
                market,
                "tushare" if supported and tushare_ready else None,
                "tushare" if supported and tushare_ready else None,
                supported and tushare_ready,
                notes,
            )
        )
    return rows


def yfinance_coverage(yfinance_ready: bool) -> list[dict]:
    rows: list[dict] = []
    for market in all_markets():
        supported = market in {Market.HK, Market.HK_FUT, Market.US, Market.US_FUT}
        notes: list[str] = []
        if supported:
            notes.append("instrument master refresh is not supported; use Tushare or seed fallback")
        else:
            notes.append("yfinance route is not configured for this market")
        if supported and not yfinance_ready:
            notes.append("requires installed yfinance package")
        rows.append(
            coverage_row(
                market,
                "yfinance" if supported and yfinance_ready else None,
                None,
                False,
                notes,
            )
        )
    return rows


def coverage_row(
    market: Market,
    price_provider: str | None,
    instrument_master_provider: str | None,
    real_instrument_master: bool,
    notes: list[str],
) -> dict:
    return {
        "market": market.value,
        "price_provider": price_provider,
        "price_available": price_provider is not None,
        "instrument_master_provider": instrument_master_provider,
        "real_instrument_master": real_instrument_master,
        "notes": [note for note in notes if note],
    }


def missing_dependency_note(market: Market, tushare_ready: bool, yfinance_ready: bool) -> str:
    if market in {Market.CN_A, Market.CN_FUT}:
        return "requires TUSHARE_TOKEN and installed tushare package"
    if market in {Market.HK_FUT, Market.US_FUT}:
        return "requires installed yfinance package"
    if market in {Market.HK, Market.US}:
        if not tushare_ready and not yfinance_ready:
            return "requires TUSHARE_TOKEN+tushare or installed yfinance package"
    return "no configured price provider"


def dependency_available(package_name: str) -> bool:
    return find_spec(package_name) is not None


def all_markets() -> tuple[Market, ...]:
    return (Market.CN_A, Market.HK, Market.CN_FUT, Market.HK_FUT, Market.US, Market.US_FUT)
