"""SQLite storage for Portfolio Signal.

One module owns the schema and the connection helper so the webhook receiver, the
agent, and the pipeline all write the same shape. Stdlib ``sqlite3`` only.

Timestamps are stored as ISO-8601 UTC text (``YYYY-MM-DD HH:MM:SS``) because that
is what SQLite's ``date()`` / ``datetime()`` functions understand directly, which
keeps the Phase 3 rollup SQL readable.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Tables are created if missing on every connect(); this is the whole schema.
SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id              TEXT PRIMARY KEY,
    name                    TEXT NOT NULL,
    plan_minutes_limit      INTEGER,
    plan_concurrency_limit  INTEGER
);

-- Raw LiveKit webhook events, one row per delivery.
-- source: 'live' = a real LiveKit Cloud webhook; 'sim' = scripts/simulate_accounts.py
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,          -- ISO-8601 UTC
    type            TEXT NOT NULL,          -- e.g. room_started, participant_left
    room_sid        TEXT,
    room_name       TEXT,
    account_id      TEXT,
    participant_id  TEXT,
    raw_json        TEXT NOT NULL,          -- the exact verified webhook body
    source          TEXT NOT NULL DEFAULT 'sim'
);
CREATE INDEX IF NOT EXISTS ix_events_account_ts ON events (account_id, ts);
CREATE INDEX IF NOT EXISTS ix_events_room       ON events (room_sid);
CREATE INDEX IF NOT EXISTS ix_events_type_ts    ON events (type, ts);

-- One row per conversational turn (user utterance -> agent reply), assembled
-- from the agent's metrics_collected events by shared speech_id.
CREATE TABLE IF NOT EXISTS turn_metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,        -- ISO-8601 UTC (turn end)
    room_sid          TEXT,
    account_id        TEXT,
    speech_id         TEXT,                 -- correlation key from the SDK
    ttft_ms           REAL,                 -- LLM time-to-first-token
    ttfb_ms           REAL,                 -- TTS time-to-first-byte
    eou_ms            REAL,                 -- end-of-utterance delay
    total_latency_ms  REAL,                 -- derived, see DESIGN.md
    llm_tokens_in     INTEGER,
    llm_tokens_out    INTEGER,
    error_flag        INTEGER NOT NULL DEFAULT 0,
    source            TEXT NOT NULL DEFAULT 'sim'   -- 'live' = a real agent call
);
CREATE INDEX IF NOT EXISTS ix_turn_account_ts ON turn_metrics (account_id, ts);
CREATE INDEX IF NOT EXISTS ix_turn_room       ON turn_metrics (room_sid);

-- One row per connection_quality_changed event.
CREATE TABLE IF NOT EXISTS quality_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,          -- ISO-8601 UTC
    room_sid        TEXT,
    account_id      TEXT,
    participant_id  TEXT,
    quality         TEXT NOT NULL,          -- excellent | good | poor | lost | unknown
    source          TEXT NOT NULL DEFAULT 'sim'
);
CREATE INDEX IF NOT EXISTS ix_quality_account_ts ON quality_events (account_id, ts);
CREATE INDEX IF NOT EXISTS ix_quality_room       ON quality_events (room_sid);

-- Derived in Phase 3 from events; table defined here so the schema is in one place.
CREATE TABLE IF NOT EXISTS sessions (
    room_sid        TEXT PRIMARY KEY,
    account_id      TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    duration_s      REAL,
    graceful_end    INTEGER,                -- 1 / 0 / NULL if unknown
    n_participants  INTEGER,
    source          TEXT NOT NULL DEFAULT 'sim'   -- 'live' if any underlying row is live
);
CREATE INDEX IF NOT EXISTS ix_sessions_account ON sessions (account_id, started_at);
"""

# Columns added after tables shipped; connect() backfills existing DBs.
_ADDED_COLUMNS = {
    "events": [("source", "TEXT NOT NULL DEFAULT 'sim'")],
    "turn_metrics": [("source", "TEXT NOT NULL DEFAULT 'sim'")],
    "quality_events": [("source", "TEXT NOT NULL DEFAULT 'sim'")],
    "sessions": [("source", "TEXT NOT NULL DEFAULT 'sim'")],
}

TABLES = ["accounts", "events", "turn_metrics", "quality_events", "sessions"]


def default_db_path() -> str:
    """Path to the SQLite file, from ``PORTFOLIO_DB`` or the repo default."""
    return os.environ.get("PORTFOLIO_DB", "data/portfolio.db")


def iso(ts: float | int | datetime | None = None) -> str:
    """Return an ISO-8601 UTC string. Accepts a unix timestamp, a datetime, or None (=now)."""
    if ts is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(ts, datetime):
        dt = ts.astimezone(timezone.utc)
    else:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection, ensure the schema exists, and return it.

    Safe to call from multiple processes (receiver + agent): SQLite handles the
    locking, and this project's write volume is tiny.
    """
    path = db_path or default_db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns that shipped after their table, and backfill the real rows
    written before `source` existed (a room_sid like 'RM_...' is a real LiveKit
    room; 'SIM_...' is synthetic)."""
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                if name == "source":
                    conn.execute(
                        f"UPDATE {table} SET source = 'live' WHERE room_sid GLOB 'RM_*'"
                    )


def summarise(db_path: str | None = None) -> str:
    """Human-readable row counts + a peek at the newest rows. Used at the Phase 2 checkpoint."""
    conn = connect(db_path)
    lines = [f"portfolio.db  ({db_path or default_db_path()})", "=" * 60]
    for table in TABLES:
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        lines.append(f"{table:<16} {n:>6} rows")
    for table, order in [
        ("events", "id"),
        ("turn_metrics", "id"),
        ("quality_events", "id"),
    ]:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY {order} DESC LIMIT 5"
        ).fetchall()
        if not rows:
            continue
        lines += ["", f"--- last {len(rows)} {table} ---"]
        for r in rows:
            lines.append("  " + " | ".join(f"{k}={r[k]!r}" for k in r.keys()))
    conn.close()
    return "\n".join(lines)


if __name__ == "__main__":
    # `python -m pipeline.db` creates the DB and prints a summary.
    print(summarise())
