from __future__ import annotations

import re
from difflib import SequenceMatcher

from .models import AssetType, Instrument, Market, Resolution


SEED_INSTRUMENTS: tuple[Instrument, ...] = (
    Instrument(
        symbol="300750.SZ",
        provider_symbol="300750.SZ",
        name="宁德时代",
        aliases=("CATL", "宁德", "300750", "300750.SZ"),
        market=Market.CN_A,
        asset_type=AssetType.STOCK,
        exchange="SZSE",
        currency="CNY",
        timezone="Asia/Shanghai",
    ),
    Instrument(
        symbol="600519.SH",
        provider_symbol="600519.SH",
        name="贵州茅台",
        aliases=("茅台", "贵州茅台酒", "600519", "600519.SH"),
        market=Market.CN_A,
        asset_type=AssetType.STOCK,
        exchange="SSE",
        currency="CNY",
        timezone="Asia/Shanghai",
    ),
    Instrument(
        symbol="00700.HK",
        provider_symbol="00700.HK",
        name="腾讯控股",
        aliases=("腾讯", "Tencent", "700.HK", "00700", "0700.HK"),
        market=Market.HK,
        asset_type=AssetType.STOCK,
        exchange="HKEX",
        currency="HKD",
        timezone="Asia/Hong_Kong",
    ),
    Instrument(
        symbol="09988.HK",
        provider_symbol="09988.HK",
        name="阿里巴巴-W",
        aliases=("阿里", "阿里巴巴", "Alibaba", "9988.HK", "09988"),
        market=Market.HK,
        asset_type=AssetType.STOCK,
        exchange="HKEX",
        currency="HKD",
        timezone="Asia/Hong_Kong",
    ),
    Instrument(
        symbol="CU.SHF",
        provider_symbol="CU.SHF",
        name="沪铜主连",
        aliases=("铜", "沪铜", "铜主连", "CU", "CU.SHF"),
        market=Market.CN_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="SHFE",
        currency="CNY",
        timezone="Asia/Shanghai",
        metadata={"continuous": True, "root": "CU"},
    ),
    Instrument(
        symbol="IF.CFX",
        provider_symbol="IF.CFX",
        name="沪深300股指期货主连",
        aliases=("IF", "沪深300股指期货", "IF主连", "股指期货"),
        market=Market.CN_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="CFFEX",
        currency="CNY",
        timezone="Asia/Shanghai",
        metadata={"continuous": True, "root": "IF"},
    ),
    Instrument(
        symbol="AAPL",
        provider_symbol="AAPL",
        name="Apple Inc.",
        aliases=("Apple", "苹果", "AAPL.US"),
        market=Market.US,
        asset_type=AssetType.STOCK,
        exchange="NASDAQ",
        currency="USD",
        timezone="America/New_York",
    ),
    Instrument(
        symbol="NVDA",
        provider_symbol="NVDA",
        name="NVIDIA Corp.",
        aliases=("NVIDIA", "英伟达", "NVDA.US"),
        market=Market.US,
        asset_type=AssetType.STOCK,
        exchange="NASDAQ",
        currency="USD",
        timezone="America/New_York",
    ),
    Instrument(
        symbol="ES",
        provider_symbol="ES=F",
        name="E-mini S&P 500 Futures",
        aliases=("ES", "ES=F", "标普期货", "S&P500 futures"),
        market=Market.US_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="CME",
        currency="USD",
        timezone="America/Chicago",
        metadata={"continuous": True, "root": "ES"},
    ),
    Instrument(
        symbol="NQ",
        provider_symbol="NQ=F",
        name="E-mini Nasdaq 100 Futures",
        aliases=("NQ", "NQ=F", "纳指期货", "NASDAQ futures"),
        market=Market.US_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="CME",
        currency="USD",
        timezone="America/Chicago",
        metadata={"continuous": True, "root": "NQ"},
    ),
)


class InstrumentResolver:
    def __init__(self, instruments: list[Instrument] | None = None):
        self.instruments = instruments or list(SEED_INSTRUMENTS)

    def resolve(self, raw: str, market_hint: Market | None = None) -> Resolution | None:
        query = normalize(raw)
        if not query:
            return None

        candidates = [
            instrument for instrument in self.instruments
            if market_hint is None or instrument.market == market_hint
        ]

        for instrument in candidates:
            keys = [instrument.symbol, instrument.provider_symbol, instrument.name, *instrument.aliases]
            if query in {normalize(key) for key in keys}:
                return Resolution(instrument, 1.0, "exact symbol/name/alias match")

        inferred = infer_symbol(raw)
        if inferred:
            for instrument in candidates:
                if normalize(instrument.symbol) == normalize(inferred):
                    return Resolution(instrument, 0.92, "normalized market symbol match")

        best: tuple[float, Instrument] | None = None
        for instrument in candidates:
            keys = [instrument.symbol, instrument.provider_symbol, instrument.name, *instrument.aliases]
            score = max(SequenceMatcher(None, query, normalize(key)).ratio() for key in keys)
            if best is None or score > best[0]:
                best = (score, instrument)

        if best and best[0] >= 0.62:
            return Resolution(best[1], round(best[0], 3), "fuzzy name/alias match")
        return None


def normalize(value: str) -> str:
    return re.sub(r"[\s_\-。．.]+", "", value.strip().upper())


def infer_symbol(value: str) -> str | None:
    raw = value.strip().upper()
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("0", "3")):
            return f"{raw}.SZ"
        if raw.startswith(("6", "9")):
            return f"{raw}.SH"
    if re.fullmatch(r"\d{1,5}\.HK", raw):
        number = raw.split(".", 1)[0].zfill(5)
        return f"{number}.HK"
    if re.fullmatch(r"[A-Z]{1,5}(\.US)?", raw):
        return raw.removesuffix(".US")
    return None

