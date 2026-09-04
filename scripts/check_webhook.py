"""Is the LiveKit webhook path actually delivering? Prints a verdict from what's
in the DB.

    python scripts/check_webhook.py

Expected once it's wired: `events` has rows with source='live' whose type is
room_started / participant_joined / participant_left / room_finished, and derived
sessions for real rooms carry a real graceful_end (1/0) instead of NULL.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db import connect  # noqa: E402
from pipeline.sessions import derive_sessions  # noqa: E402


def main() -> None:
    conn = connect()
    live_events = conn.execute("SELECT COUNT(*) FROM events WHERE source = 'live'").fetchone()[0]
    by_type = conn.execute(
        "SELECT type, COUNT(*) FROM events WHERE source = 'live' GROUP BY type ORDER BY 2 DESC"
    ).fetchall()

    derive_sessions(conn)
    live_sessions = conn.execute(
        "SELECT graceful_end, COUNT(*) FROM sessions WHERE source = 'live' GROUP BY graceful_end"
    ).fetchall()
    conn.close()

    print(f"live webhook events: {live_events}")
    for t, n in by_type:
        print(f"  {t:<32} {n}")
    print("\nlive sessions by graceful_end:")
    for g, n in live_sessions:
        label = {1: "graceful", 0: "ungraceful", None: "unknown (no webhook events)"}.get(g, str(g))
        print(f"  {label:<32} {n}")

    if live_events == 0:
        print("\n=> webhook path NOT delivering yet. Check: receiver + tunnel running "
              "(scripts/webhook_dev.py), URL set in the LiveKit dashboard, then run a call.")
    else:
        print("\n=> webhook path is delivering. Real sessions now get a real graceful/ungraceful verdict.")


if __name__ == "__main__":
    main()
