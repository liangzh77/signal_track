from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import AssetType, DailyBar, Instrument, Market


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS instruments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL UNIQUE,
  provider_symbol TEXT NOT NULL,
  name TEXT NOT NULL,
  aliases TEXT NOT NULL DEFAULT '[]',
  market TEXT NOT NULL,
  asset_type TEXT NOT NULL,
  exchange TEXT NOT NULL,
  currency TEXT NOT NULL,
  timezone TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_instruments_market ON instruments(market);
CREATE INDEX IF NOT EXISTS idx_instruments_name ON instruments(name);

CREATE TABLE IF NOT EXISTS price_bars (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  bar_date TEXT NOT NULL,
  open REAL,
  high REAL,
  low REAL,
  close REAL,
  adj_close REAL,
  volume REAL,
  amount REAL,
  settle REAL,
  open_interest REAL,
  provider TEXT NOT NULL,
  provider_symbol TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(instrument_id, bar_date, provider),
  FOREIGN KEY(instrument_id) REFERENCES instruments(id)
);

CREATE TABLE IF NOT EXISTS sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  source_type TEXT NOT NULL DEFAULT 'manual',
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS raw_inputs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER NOT NULL,
  received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  content TEXT NOT NULL,
  attachment_path TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(source_id) REFERENCES sources(id)
);

CREATE TABLE IF NOT EXISTS tracking_projects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  source_id INTEGER NOT NULL,
  raw_input_id INTEGER,
  status TEXT NOT NULL,
  direction TEXT NOT NULL,
  entry_date TEXT,
  closed_date TEXT,
  logic_score REAL NOT NULL DEFAULT 0,
  needs_review INTEGER NOT NULL DEFAULT 0,
  weight_needs_review INTEGER NOT NULL DEFAULT 0,
  metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(source_id) REFERENCES sources(id),
  FOREIGN KEY(raw_input_id) REFERENCES raw_inputs(id)
);

CREATE TABLE IF NOT EXISTS project_legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  instrument_id INTEGER NOT NULL,
  direction TEXT NOT NULL,
  weight REAL NOT NULL DEFAULT 1,
  entry_price REAL,
  entry_date TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY(project_id) REFERENCES tracking_projects(id),
  FOREIGN KEY(instrument_id) REFERENCES instruments(id)
);

CREATE TABLE IF NOT EXISTS logic_blocks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  logic_type TEXT NOT NULL,
  content TEXT NOT NULL,
  confidence REAL NOT NULL DEFAULT 0,
  evidence TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  FOREIGN KEY(project_id) REFERENCES tracking_projects(id)
);

CREATE TABLE IF NOT EXISTS daily_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id INTEGER NOT NULL,
  check_date TEXT NOT NULL,
  conclusion TEXT NOT NULL,
  summary TEXT NOT NULL,
  triggered_rules TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(project_id, check_date),
  FOREIGN KEY(project_id) REFERENCES tracking_projects(id)
);

CREATE TABLE IF NOT EXISTS publish_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  title TEXT NOT NULL,
  url TEXT,
  status_code INTEGER,
  response_body TEXT,
  metadata TEXT NOT NULL DEFAULT '{}'
);
"""

CURRENT_SCHEMA_VERSION = 1

REQUIRED_COLUMNS: dict[str, dict[str, str]] = {
    "raw_inputs": {
        "attachment_path": "TEXT",
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
    },
    "tracking_projects": {
        "entry_date": "TEXT",
        "closed_date": "TEXT",
        "logic_score": "REAL NOT NULL DEFAULT 0",
        "needs_review": "INTEGER NOT NULL DEFAULT 0",
        "weight_needs_review": "INTEGER NOT NULL DEFAULT 0",
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
        "updated_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    },
    "project_legs": {
        "entry_price": "REAL",
        "entry_date": "TEXT",
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
    },
    "daily_checks": {
        "triggered_rules": "TEXT NOT NULL DEFAULT '[]'",
    },
    "publish_events": {
        "url": "TEXT",
        "status_code": "INTEGER",
        "response_body": "TEXT",
        "metadata": "TEXT NOT NULL DEFAULT '{}'",
    },
}


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init(self) -> None:
        with self.session() as conn:
            conn.executescript(SCHEMA)
            migrate_connection(conn)

    def migrate(self) -> int:
        with self.session() as conn:
            return migrate_connection(conn)

    def backup(self, destination: str | Path) -> Path:
        dest_path = Path(destination)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        try:
            target = sqlite3.connect(dest_path)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return dest_path


def migrate_connection(conn: sqlite3.Connection) -> int:
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")
    for table, columns in REQUIRED_COLUMNS.items():
        existing = table_columns(conn, table)
        if not existing:
            continue
        for column, definition in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    return CURRENT_SCHEMA_VERSION


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return set()
    return {row["name"] if isinstance(row, sqlite3.Row) else row[1] for row in rows}


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def upsert_instrument(self, instrument: Instrument) -> int:
        with self.db.session() as conn:
            conn.execute(
                """
                INSERT INTO instruments (
                  symbol, provider_symbol, name, aliases, market, asset_type,
                  exchange, currency, timezone, status, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                  provider_symbol=excluded.provider_symbol,
                  name=excluded.name,
                  aliases=excluded.aliases,
                  market=excluded.market,
                  asset_type=excluded.asset_type,
                  exchange=excluded.exchange,
                  currency=excluded.currency,
                  timezone=excluded.timezone,
                  status=excluded.status,
                  metadata=excluded.metadata,
                  updated_at=CURRENT_TIMESTAMP
                """,
                (
                    instrument.symbol,
                    instrument.provider_symbol,
                    instrument.name,
                    json.dumps(list(instrument.aliases), ensure_ascii=False),
                    instrument.market.value,
                    instrument.asset_type.value,
                    instrument.exchange,
                    instrument.currency,
                    instrument.timezone,
                    instrument.status,
                    json.dumps(instrument.metadata, ensure_ascii=False),
                ),
            )
            row = conn.execute(
                "SELECT id FROM instruments WHERE symbol = ?", (instrument.symbol,)
            ).fetchone()
            return int(row["id"])

    def get_instrument(self, symbol: str) -> Instrument | None:
        with self.db.session() as conn:
            row = conn.execute(
                "SELECT * FROM instruments WHERE symbol = ?", (symbol,)
            ).fetchone()
        return row_to_instrument(row) if row else None

    def list_instruments(self) -> list[Instrument]:
        with self.db.session() as conn:
            rows = conn.execute("SELECT * FROM instruments ORDER BY market, symbol").fetchall()
        return [row_to_instrument(row) for row in rows]

    def upsert_bars(self, instrument_id: int, bars: Iterable[DailyBar]) -> int:
        count = 0
        with self.db.session() as conn:
            for bar in bars:
                conn.execute(
                    """
                    INSERT INTO price_bars (
                      instrument_id, bar_date, open, high, low, close, adj_close,
                      volume, amount, settle, open_interest, provider, provider_symbol
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(instrument_id, bar_date, provider) DO UPDATE SET
                      open=excluded.open,
                      high=excluded.high,
                      low=excluded.low,
                      close=excluded.close,
                      adj_close=excluded.adj_close,
                      volume=excluded.volume,
                      amount=excluded.amount,
                      settle=excluded.settle,
                      open_interest=excluded.open_interest,
                      provider_symbol=excluded.provider_symbol
                    """,
                    (
                        instrument_id,
                        bar.date.isoformat(),
                        bar.open,
                        bar.high,
                        bar.low,
                        bar.close,
                        bar.adj_close,
                        bar.volume,
                        bar.amount,
                        bar.settle,
                        bar.open_interest,
                        bar.provider,
                        bar.provider_symbol or bar.symbol,
                    ),
                )
                count += 1
        return count

    def count_price_bars(self, symbol: str) -> int:
        with self.db.session() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM price_bars pb
                JOIN instruments i ON i.id = pb.instrument_id
                WHERE i.symbol = ?
                """,
                (symbol,),
            ).fetchone()
        return int(row["count"])

    def get_or_create_source(self, name: str, source_type: str = "manual") -> int:
        with self.db.session() as conn:
            conn.execute(
                """
                INSERT INTO sources(name, source_type)
                VALUES (?, ?)
                ON CONFLICT(name) DO UPDATE SET source_type=excluded.source_type
                """,
                (name, source_type),
            )
            row = conn.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
            return int(row["id"])

    def add_raw_input(
        self,
        source_id: int,
        content: str,
        attachment_path: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        with self.db.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO raw_inputs(source_id, content, attachment_path, metadata)
                VALUES (?, ?, ?, ?)
                """,
                (
                    source_id,
                    content,
                    attachment_path,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def create_tracking_project(
        self,
        title: str,
        source_id: int,
        raw_input_id: int | None,
        status: str,
        direction: str,
        logic_score: float,
        entry_date: str | None = None,
        needs_review: bool = False,
        weight_needs_review: bool = False,
        metadata: dict | None = None,
    ) -> int:
        with self.db.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO tracking_projects(
                  title, source_id, raw_input_id, status, direction, entry_date, logic_score,
                  needs_review, weight_needs_review, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    title,
                    source_id,
                    raw_input_id,
                    status,
                    direction,
                    entry_date,
                    logic_score,
                    int(needs_review),
                    int(weight_needs_review),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def add_project_leg(
        self,
        project_id: int,
        instrument_id: int,
        direction: str,
        weight: float,
        metadata: dict | None = None,
    ) -> int:
        with self.db.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO project_legs(project_id, instrument_id, direction, weight, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    instrument_id,
                    direction,
                    weight,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def update_leg_entry(
        self,
        leg_id: int,
        entry_price: float,
        entry_date: str,
    ) -> None:
        with self.db.session() as conn:
            conn.execute(
                """
                UPDATE project_legs
                SET entry_price = ?, entry_date = ?
                WHERE id = ?
                """,
                (entry_price, entry_date, leg_id),
            )

    def update_project_status(self, project_id: int, status: str, needs_review: bool | None = None) -> None:
        assignments = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[object] = [status]
        if needs_review is not None:
            assignments.append("needs_review = ?")
            params.append(int(needs_review))
        params.append(project_id)
        with self.db.session() as conn:
            conn.execute(
                f"UPDATE tracking_projects SET {', '.join(assignments)} WHERE id = ?",
                params,
            )

    def close_project(self, project_id: int, closed_date: str, metadata: dict | None = None) -> None:
        with self.db.session() as conn:
            conn.execute(
                """
                UPDATE tracking_projects
                SET status = 'closed',
                    closed_date = ?,
                    needs_review = 0,
                    metadata = CASE
                      WHEN ? IS NULL THEN metadata
                      ELSE ?
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    closed_date,
                    json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                    json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                    project_id,
                ),
            )

    def add_logic_block(
        self,
        project_id: int,
        logic_type: str,
        content: str,
        confidence: float,
        evidence: list[str] | None = None,
    ) -> int:
        with self.db.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO logic_blocks(project_id, logic_type, content, confidence, evidence)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    logic_type,
                    content,
                    confidence,
                    json.dumps(evidence or [], ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)

    def list_project_rows(self) -> list[sqlite3.Row]:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT
                  p.*,
                  s.name AS source_name,
                  GROUP_CONCAT(i.symbol, ', ') AS symbols,
                  GROUP_CONCAT(i.name, ', ') AS instrument_names
                FROM tracking_projects p
                JOIN sources s ON s.id = p.source_id
                LEFT JOIN project_legs l ON l.project_id = p.id
                LEFT JOIN instruments i ON i.id = l.instrument_id
                GROUP BY p.id
                ORDER BY p.updated_at DESC, p.id DESC
                """
            ).fetchall()

    def list_active_project_ids(self) -> list[int]:
        with self.db.session() as conn:
            rows = conn.execute(
                """
                SELECT id FROM tracking_projects
                WHERE status IN ('active', 'needs_review', 'exit_signal')
                ORDER BY id
                """
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def find_active_project_ids_by_symbol(self, symbol: str) -> list[int]:
        with self.db.session() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT p.id
                FROM tracking_projects p
                JOIN project_legs l ON l.project_id = p.id
                JOIN instruments i ON i.id = l.instrument_id
                WHERE i.symbol = ?
                  AND p.status IN ('active', 'needs_review', 'exit_signal')
                ORDER BY p.id
                """,
                (symbol,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def get_project_row(self, project_id: int) -> sqlite3.Row | None:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT p.*, s.name AS source_name
                FROM tracking_projects p
                JOIN sources s ON s.id = p.source_id
                WHERE p.id = ?
                """,
                (project_id,),
            ).fetchone()

    def list_project_legs(self, project_id: int) -> list[sqlite3.Row]:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT
                  l.*,
                  i.symbol,
                  i.provider_symbol,
                  i.name,
                  i.aliases,
                  i.market,
                  i.asset_type,
                  i.exchange,
                  i.currency,
                  i.timezone,
                  i.status AS instrument_status,
                  i.metadata AS instrument_metadata
                FROM project_legs l
                JOIN instruments i ON i.id = l.instrument_id
                WHERE l.project_id = ?
                ORDER BY l.id
                """,
                (project_id,),
            ).fetchall()

    def list_logic_blocks(self, project_id: int) -> list[sqlite3.Row]:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT * FROM logic_blocks
                WHERE project_id = ?
                ORDER BY logic_type, id
                """,
                (project_id,),
            ).fetchall()

    def list_price_bars(
        self,
        instrument_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[sqlite3.Row]:
        where = ["instrument_id = ?"]
        params: list[object] = [instrument_id]
        if start_date:
            where.append("bar_date >= ?")
            params.append(start_date)
        if end_date:
            where.append("bar_date <= ?")
            params.append(end_date)
        with self.db.session() as conn:
            return conn.execute(
                f"""
                SELECT * FROM price_bars
                WHERE {' AND '.join(where)}
                ORDER BY bar_date
                """,
                params,
            ).fetchall()

    def get_first_price_on_or_after(self, instrument_id: int, start_date: str) -> sqlite3.Row | None:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT * FROM price_bars
                WHERE instrument_id = ? AND bar_date >= ? AND close IS NOT NULL
                ORDER BY bar_date ASC
                LIMIT 1
                """,
                (instrument_id, start_date),
            ).fetchone()

    def get_latest_price_on_or_before(self, instrument_id: int, end_date: str) -> sqlite3.Row | None:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT * FROM price_bars
                WHERE instrument_id = ? AND bar_date <= ? AND close IS NOT NULL
                ORDER BY bar_date DESC
                LIMIT 1
                """,
                (instrument_id, end_date),
            ).fetchone()

    def add_daily_check(
        self,
        project_id: int,
        check_date: str,
        conclusion: str,
        summary: str,
        triggered_rules: list[str] | None = None,
    ) -> None:
        with self.db.session() as conn:
            conn.execute(
                """
                INSERT INTO daily_checks(project_id, check_date, conclusion, summary, triggered_rules)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, check_date) DO UPDATE SET
                  conclusion=excluded.conclusion,
                  summary=excluded.summary,
                  triggered_rules=excluded.triggered_rules,
                  created_at=CURRENT_TIMESTAMP
                """,
                (
                    project_id,
                    check_date,
                    conclusion,
                    summary,
                    json.dumps(triggered_rules or [], ensure_ascii=False),
                ),
            )

    def list_daily_checks(self, limit: int = 30, project_id: int | None = None) -> list[sqlite3.Row]:
        where = ""
        params: list[object] = []
        if project_id is not None:
            where = "WHERE c.project_id = ?"
            params.append(project_id)
        params.append(limit)
        with self.db.session() as conn:
            return conn.execute(
                f"""
                SELECT c.*, p.title
                FROM daily_checks c
                JOIN tracking_projects p ON p.id = c.project_id
                {where}
                ORDER BY c.check_date DESC, c.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

    def list_publish_events(self, limit: int = 20) -> list[sqlite3.Row]:
        with self.db.session() as conn:
            return conn.execute(
                """
                SELECT * FROM publish_events
                ORDER BY published_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def record_publish_event(
        self,
        title: str,
        url: str | None,
        status_code: int | None,
        response_body: str | None,
        metadata: dict | None = None,
    ) -> int:
        with self.db.session() as conn:
            cur = conn.execute(
                """
                INSERT INTO publish_events(title, url, status_code, response_body, metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    title,
                    url,
                    status_code,
                    response_body,
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            return int(cur.lastrowid)


def row_to_instrument(row: sqlite3.Row) -> Instrument:
    return Instrument(
        id=int(row["id"]),
        symbol=row["symbol"],
        provider_symbol=row["provider_symbol"],
        name=row["name"],
        aliases=tuple(json.loads(row["aliases"])),
        market=Market(row["market"]),
        asset_type=AssetType(row["asset_type"]),
        exchange=row["exchange"],
        currency=row["currency"],
        timezone=row["timezone"],
        status=row["status"],
        metadata=json.loads(row["metadata"]),
    )
