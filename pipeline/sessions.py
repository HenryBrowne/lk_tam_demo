"""Derive the ``sessions`` table from raw telemetry.

``sessions`` is a pure projection - this rebuilds it from scratch every run
(DELETE + INSERT). Primary source is the ``events`` table (LiveKit webhooks).
For rooms that have ``turn_metrics`` / ``quality_events`` but no ``events`` yet
(a real agent call before the webhook path is wired up), a minimal session is
synthesized with ``graceful_end = NULL``. Both cases are approximations and are
documented as such in DESIGN.md.

    python -m pipeline.sessions
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from pipeline.db import connect

_TS_FMT = "%Y-%m-%d %H:%M:%S"
# disconnect_reason values we treat as a clean hang-up (everything else = ungraceful).
# Defensive: NOTES-livekit-api.md flags that the webhook payload's disconnect_reason
# is still unverified on LiveKit Cloud.
_GRACEFUL_REASONS = {"", "CLIENT_INITIATED", "USER_INITIATED"}


def _secs(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.strptime(end, _TS_FMT) - datetime.strptime(start, _TS_FMT)).total_seconds()


def _is_agent(identity: str | None) -> bool:
    return bool(identity) and identity.startswith("agent-")


def _reason_from_raw(raw_json: str) -> str | None:
    try:
        data = json.loads(raw_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    part = data.get("participant") or {}
    return part.get("disconnect_reason") or data.get("disconnect_reason")


def _session_from_events(room_sid: str, rows: list[sqlite3.Row]) -> dict:
    rows = sorted(rows, key=lambda r: r["ts"])
    types = [r["type"] for r in rows]
    starts = [r["ts"] for r in rows if r["type"] == "room_started"] or [rows[0]["ts"]]
    ends = [r["ts"] for r in rows if r["type"] == "room_finished"] or [rows[-1]["ts"]]
    started_at, ended_at = starts[0], ends[-1]

    humans = {
        r["participant_id"]
        for r in rows
        if r["type"] == "participant_joined" and not _is_agent(r["participant_id"])
    }

    graceful: int | None
    aborted = "participant_connection_aborted" in types
    bad_reason = any(
        r["type"] == "participant_left"
        and (_reason_from_raw(r["raw_json"] or "") or "") not in _GRACEFUL_REASONS
        for r in rows
    )
    if aborted or bad_reason:
        graceful = 0
    elif "room_finished" in types or "participant_left" in types:
        graceful = 1
    else:
        graceful = None

    return {
        "room_sid": room_sid,
        "account_id": next((r["account_id"] for r in rows if r["account_id"]), None),
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_s": _secs(started_at, ended_at),
        "graceful_end": graceful,
        "n_participants": len(humans) or 1,
    }


def _session_fallback(room_sid: str, conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT MIN(ts) AS start_ts, MAX(ts) AS end_ts,
               (SELECT account_id FROM turn_metrics WHERE room_sid = :r AND account_id IS NOT NULL LIMIT 1) AS acc1,
               (SELECT account_id FROM quality_events WHERE room_sid = :r AND account_id IS NOT NULL LIMIT 1) AS acc2
        FROM (
            SELECT ts, account_id FROM turn_metrics   WHERE room_sid = :r
            UNION ALL
            SELECT ts, account_id FROM quality_events WHERE room_sid = :r
        )
        """,
        {"r": room_sid},
    ).fetchone()
    parts = {
        p[0]
        for p in conn.execute(
            "SELECT DISTINCT participant_id FROM quality_events WHERE room_sid = ?", (room_sid,)
        ).fetchall()
        if p[0] and not _is_agent(p[0])
    }
    return {
        "room_sid": room_sid,
        "account_id": row["acc1"] or row["acc2"],
        "started_at": row["start_ts"],
        "ended_at": row["end_ts"],
        "duration_s": _secs(row["start_ts"], row["end_ts"]),
        "graceful_end": None,  # unknown without events
        "n_participants": len(parts) or 1,
    }


def derive_sessions(conn: sqlite3.Connection) -> int:
    """Rebuild ``sessions`` from ``events`` (+ fallback). Returns the row count."""
    conn.execute("DELETE FROM sessions")

    event_rooms: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT * FROM events WHERE room_sid IS NOT NULL ORDER BY ts"):
        event_rooms.setdefault(r["room_sid"], []).append(r)

    other_rooms = {
        r[0]
        for r in conn.execute(
            """
            SELECT room_sid FROM turn_metrics WHERE room_sid IS NOT NULL
            UNION
            SELECT room_sid FROM quality_events WHERE room_sid IS NOT NULL
            """
        )
        if r[0] not in event_rooms
    }

    sessions = [_session_from_events(sid, rows) for sid, rows in event_rooms.items()]
    sessions += [_session_fallback(sid, conn) for sid in sorted(other_rooms)]

    conn.executemany(
        """
        INSERT INTO sessions (room_sid, account_id, started_at, ended_at, duration_s,
                              graceful_end, n_participants)
        VALUES (:room_sid, :account_id, :started_at, :ended_at, :duration_s,
                :graceful_end, :n_participants)
        """,
        sessions,
    )
    conn.commit()
    return len(sessions)


if __name__ == "__main__":
    conn = connect()
    n = derive_sessions(conn)
    by_verdict = conn.execute(
        "SELECT graceful_end, COUNT(*) FROM sessions GROUP BY graceful_end"
    ).fetchall()
    print(f"derived {n} sessions")
    for g, c in by_verdict:
        label = {1: "graceful", 0: "ungraceful", None: "unknown"}[g]
        print(f"  {label:<11} {c}")
    conn.close()
