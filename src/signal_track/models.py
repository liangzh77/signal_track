from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any


class Market(StrEnum):
    CN_A = "CN_A"
    HK = "HK"
    CN_FUT = "CN_FUT"
    US = "US"
    US_FUT = "US_FUT"


class AssetType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    INDEX = "index"
    FUTURE = "future"
    CONTINUOUS_FUTURE = "continuous_future"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class ProjectStatus(StrEnum):
    ACTIVE = "active"
    EXIT_SIGNAL = "exit_signal"
    CLOSED = "closed"
    WATCH_AFTER_CLOSE = "watch_after_close"
    ARCHIVED = "archived"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    provider_symbol: str
    name: str
    market: Market
    asset_type: AssetType
    exchange: str
    currency: str
    timezone: str
    aliases: tuple[str, ...] = ()
    status: str = "active"
    metadata: dict[str, Any] = field(default_factory=dict)
    id: int | None = None


@dataclass(frozen=True)
class Resolution:
    instrument: Instrument
    confidence: float
    reason: str


@dataclass(frozen=True)
class DailyBar:
    symbol: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None = None
    volume: float | None = None
    amount: float | None = None
    settle: float | None = None
    open_interest: float | None = None
    provider: str = "unknown"
    provider_symbol: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
