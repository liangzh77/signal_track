from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from .db import Repository
from .models import AssetType, Direction, Instrument, Market


@dataclass(frozen=True)
class LegPerformance:
    leg_id: int
    symbol: str
    name: str
    direction: str
    weight: float
    entry_date: str | None
    entry_price: float | None
    latest_date: str | None
    latest_price: float | None
    return_pct: float | None
    points: list[tuple[str, float]]


@dataclass(frozen=True)
class ProjectPerformance:
    project_id: int
    return_pct: float | None
    latest_date: str | None
    points: list[tuple[str, float]]
    legs: list[LegPerformance]
    missing_price_symbols: list[str]


def project_performance(repo: Repository, project_id: int, end_date: date | None = None) -> ProjectPerformance:
    project = repo.get_project_row(project_id)
    if not project:
        return ProjectPerformance(project_id, None, None, [], [], [])

    if end_date:
        current_end = end_date.isoformat()
    elif project["closed_date"]:
        current_end = (date.fromisoformat(project["closed_date"]) + timedelta(days=31)).isoformat()
    else:
        current_end = date.today().isoformat()
    entry_date = project["entry_date"] or project["created_at"][:10]
    chart_start = (date.fromisoformat(entry_date) - timedelta(days=31)).isoformat()
    legs: list[LegPerformance] = []
    missing: list[str] = []

    for leg in repo.list_project_legs(project_id):
        bars = repo.list_price_bars(int(leg["instrument_id"]), chart_start, current_end)
        entry_bar = repo.get_first_price_on_or_after(int(leg["instrument_id"]), entry_date)
        latest_bar = repo.get_latest_price_on_or_before(int(leg["instrument_id"]), current_end)
        entry_price = float(leg["entry_price"]) if leg["entry_price"] is not None else None
        leg_entry_date = leg["entry_date"]

        if entry_price is None and entry_bar and entry_bar["close"] is not None:
            entry_price = float(entry_bar["close"])
            leg_entry_date = entry_bar["bar_date"]
            repo.update_leg_entry(int(leg["id"]), entry_price, leg_entry_date)

        points = []
        for bar in bars:
            if entry_price and bar["close"] is not None:
                points.append((bar["bar_date"], directed_return(float(bar["close"]), entry_price, leg["direction"])))

        latest_price = float(latest_bar["close"]) if latest_bar and latest_bar["close"] is not None else None
        return_pct = (
            directed_return(latest_price, entry_price, leg["direction"])
            if latest_price is not None and entry_price
            else None
        )
        if return_pct is None:
            missing.append(leg["symbol"])

        legs.append(
            LegPerformance(
                leg_id=int(leg["id"]),
                symbol=leg["symbol"],
                name=leg["name"],
                direction=leg["direction"],
                weight=float(leg["weight"]),
                entry_date=leg_entry_date,
                entry_price=entry_price,
                latest_date=latest_bar["bar_date"] if latest_bar else None,
                latest_price=latest_price,
                return_pct=return_pct,
                points=points,
            )
        )

    portfolio_points = combine_weighted_points(legs)
    portfolio_return = None
    if legs and all(leg.return_pct is not None for leg in legs):
        portfolio_return = sum((leg.return_pct or 0) * leg.weight for leg in legs)
    latest_date = max((leg.latest_date for leg in legs if leg.latest_date), default=None)
    return ProjectPerformance(project_id, portfolio_return, latest_date, portfolio_points, legs, missing)


def instrument_from_leg_row(row) -> Instrument:
    return Instrument(
        id=int(row["instrument_id"]),
        symbol=row["symbol"],
        provider_symbol=row["provider_symbol"],
        name=row["name"],
        aliases=tuple(json.loads(row["aliases"])),
        market=Market(row["market"]),
        asset_type=AssetType(row["asset_type"]),
        exchange=row["exchange"],
        currency=row["currency"],
        timezone=row["timezone"],
        status=row["instrument_status"],
        metadata=json.loads(row["instrument_metadata"]),
    )


def directed_return(price: float, entry_price: float, direction: str) -> float:
    if entry_price == 0:
        return 0
    if direction == Direction.SHORT.value:
        return entry_price / price - 1
    return price / entry_price - 1


def combine_weighted_points(legs: list[LegPerformance]) -> list[tuple[str, float]]:
    by_date: dict[str, float] = {}
    weights_by_date: dict[str, float] = {}
    for leg in legs:
        for point_date, value in leg.points:
            by_date[point_date] = by_date.get(point_date, 0) + value * leg.weight
            weights_by_date[point_date] = weights_by_date.get(point_date, 0) + leg.weight
    return [
        (point_date, by_date[point_date])
        for point_date in sorted(by_date)
        if weights_by_date.get(point_date, 0) > 0
    ]
