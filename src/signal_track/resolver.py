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
        symbol="HSI",
        provider_symbol="HSI=F",
        name="Hang Seng Index Futures",
        aliases=("HSI", "HSI=F", "恒指期货", "恒生指数期货", "Hang Seng futures"),
        market=Market.HK_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="HKEX",
        currency="HKD",
        timezone="Asia/Hong_Kong",
        metadata={"continuous": True, "root": "HSI"},
    ),
    Instrument(
        symbol="HHI",
        provider_symbol="HHI=F",
        name="Hang Seng China Enterprises Index Futures",
        aliases=("HHI", "HHI=F", "国指期货", "恒生国企期货", "HSCEI futures"),
        market=Market.HK_FUT,
        asset_type=AssetType.CONTINUOUS_FUTURE,
        exchange="HKEX",
        currency="HKD",
        timezone="Asia/Hong_Kong",
        metadata={"continuous": True, "root": "HHI"},
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

        inferred = infer_symbol(raw, market_hint)
        if inferred:
            for instrument in candidates:
                if normalize(instrument.symbol) == normalize(inferred):
                    return Resolution(instrument, 0.92, "normalized market symbol match")
            synthetic = synthesize_instrument(raw, inferred, market_hint)
            if synthetic:
                return Resolution(synthetic, 0.86, "synthetic symbol pattern match")

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


def infer_symbol(value: str, market_hint: Market | None = None) -> str | None:
    raw = value.strip().upper()
    if re.fullmatch(r"\d{6}", raw):
        if raw.startswith(("0", "3")):
            return f"{raw}.SZ"
        if raw.startswith(("6", "9")):
            return f"{raw}.SH"
    if market_hint == Market.HK and re.fullmatch(r"\d{1,5}", raw):
        return raw.zfill(5) + ".HK"
    if re.fullmatch(r"\d{1,5}\.HK", raw):
        number = raw.split(".", 1)[0].zfill(5)
        return f"{number}.HK"
    if re.fullmatch(r"[A-Z]{1,4}\d{3,4}\.(SHF|DCE|CZC|CFX|INE|GFE)", raw):
        return raw
    if re.fullmatch(r"[A-Z]{1,4}\.(SHF|DCE|CZC|CFX|INE|GFE)", raw):
        return raw
    if re.fullmatch(r"[A-Z]{1,3}=F", raw):
        return raw.removesuffix("=F")
    if re.fullmatch(r"[A-Z]{1,5}(\.US)?", raw):
        return raw.removesuffix(".US")
    return None


def synthesize_instrument(raw_value: str, symbol: str, market_hint: Market | None = None) -> Instrument | None:
    raw = raw_value.strip()
    upper = raw.upper()
    if re.fullmatch(r"\d{6}\.(SZ|SH)", symbol):
        market = Market.CN_A
        if market_hint and market_hint != market:
            return None
        exchange = "SZSE" if symbol.endswith(".SZ") else "SSE"
        return Instrument(
            symbol=symbol,
            provider_symbol=symbol,
            name=symbol,
            aliases=(symbol.split(".", 1)[0], symbol),
            market=market,
            asset_type=AssetType.STOCK,
            exchange=exchange,
            currency="CNY",
            timezone="Asia/Shanghai",
            metadata={"synthetic": True},
        )
    if re.fullmatch(r"\d{5}\.HK", symbol):
        market = Market.HK
        if market_hint and market_hint != market:
            return None
        return Instrument(
            symbol=symbol,
            provider_symbol=symbol,
            name=symbol,
            aliases=(symbol.lstrip("0"), symbol),
            market=market,
            asset_type=AssetType.STOCK,
            exchange="HKEX",
            currency="HKD",
            timezone="Asia/Hong_Kong",
            metadata={"synthetic": True},
        )
    if re.fullmatch(r"[A-Z]{1,4}\d{3,4}\.(SHF|DCE|CZC|CFX|INE|GFE)", symbol):
        if market_hint and market_hint != Market.CN_FUT:
            return None
        exchange = cn_future_exchange(symbol.rsplit(".", 1)[1])
        return Instrument(
            symbol=symbol,
            provider_symbol=symbol,
            name=symbol,
            aliases=(symbol,),
            market=Market.CN_FUT,
            asset_type=AssetType.FUTURE,
            exchange=exchange,
            currency="CNY",
            timezone="Asia/Shanghai",
            metadata={"synthetic": True},
        )
    if re.fullmatch(r"[A-Z]{1,4}\.(SHF|DCE|CZC|CFX|INE|GFE)", symbol):
        if market_hint and market_hint != Market.CN_FUT:
            return None
        suffix = symbol.rsplit(".", 1)[1]
        root = symbol.split(".", 1)[0]
        return Instrument(
            symbol=symbol,
            provider_symbol=symbol,
            name=f"{root} continuous future",
            aliases=(root, symbol),
            market=Market.CN_FUT,
            asset_type=AssetType.CONTINUOUS_FUTURE,
            exchange=cn_future_exchange(suffix),
            currency="CNY",
            timezone="Asia/Shanghai",
            metadata={"synthetic": True, "continuous": True, "root": root},
        )
    if is_hk_future_root(symbol) and (re.fullmatch(r"[A-Z]{2,3}=F", upper) or market_hint == Market.HK_FUT):
        root = symbol.removesuffix("=F")
        return Instrument(
            symbol=root,
            provider_symbol=f"{root}=F",
            name=f"{root} Hong Kong continuous future",
            aliases=(root, f"{root}=F"),
            market=Market.HK_FUT,
            asset_type=AssetType.CONTINUOUS_FUTURE,
            exchange="HKEX",
            currency="HKD",
            timezone="Asia/Hong_Kong",
            metadata={"synthetic": True, "continuous": True, "root": root},
        )
    if re.fullmatch(r"[A-Z]{1,3}=F", upper) or market_hint == Market.US_FUT:
        root = symbol.removesuffix("=F")
        if not re.fullmatch(r"[A-Z]{1,3}", root):
            return None
        return Instrument(
            symbol=root,
            provider_symbol=f"{root}=F",
            name=f"{root} continuous future",
            aliases=(root, f"{root}=F"),
            market=Market.US_FUT,
            asset_type=AssetType.CONTINUOUS_FUTURE,
            exchange="CME",
            currency="USD",
            timezone="America/Chicago",
            metadata={"synthetic": True, "continuous": True, "root": root},
        )
    if market_hint not in {None, Market.US}:
        return None
    if not re.fullmatch(r"[A-Z]{1,5}(\.US)?", upper):
        return None
    if not (raw == upper or upper.endswith(".US") or market_hint == Market.US):
        return None
    symbol = upper.removesuffix(".US")
    if symbol in {"LONG", "SHORT", "BUY", "SELL", "HOLD", "WATCH", "EXIT", "CLOSE", "HK", "US", "SZ", "SH"}:
        return None
    return Instrument(
        symbol=symbol,
        provider_symbol=symbol,
        name=symbol,
        aliases=(symbol, f"{symbol}.US"),
        market=Market.US,
        asset_type=AssetType.STOCK,
        exchange="US",
        currency="USD",
        timezone="America/New_York",
        metadata={"synthetic": True},
    )


def cn_future_exchange(suffix: str) -> str:
    return {
        "SHF": "SHFE",
        "DCE": "DCE",
        "CZC": "CZCE",
        "CFX": "CFFEX",
        "INE": "INE",
        "GFE": "GFEX",
    }[suffix]


def is_hk_future_root(symbol: str) -> bool:
    return symbol.removesuffix("=F").upper() in {"HSI", "HHI", "MHI"}
