"""Seed synthetic accounts + backdated telemetry so the pipeline and dashboard
have a portfolio to show. There is only one real LiveKit test project, so this
fabricates 4-5 accounts with prescribed health profiles (see data/accounts.yaml).

    python scripts/simulate_accounts.py                 # --reset (default), --seed 42, --days 14
    python scripts/simulate_accounts.py --seed 7 --days 21
    python scripts/simulate_accounts.py --no-reset      # append instead of rebuild

Determinism: same --seed + --days + accounts.yaml => identical rows.
--reset only deletes rows this script owns: managed account_ids whose room_sid is
NULL or starts with 'SIM_'. Real rows (room_sid 'RM_...') are never touched.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run-by-path: put repo root on sys.path

from pipeline.db import connect  # noqa: E402

ACCOUNTS_YAML = Path(__file__).resolve().parent.parent / "data" / "accounts.yaml"
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def load_accounts() -> list[dict]:
    with ACCOUNTS_YAML.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["accounts"]


def _event_row(ts: datetime, etype: str, room_sid: str, room_name: str,
               account_id: str, participant_id: str | None, payload: dict) -> tuple:
    return (
        ts.strftime(_TS_FMT), etype, room_sid, room_name, account_id, participant_id,
        json.dumps(payload),
    )


def _gen_quality(rng: random.Random, room_sid: str, account_id: str, caller: str,
                 agent: str, start: datetime, end: datetime, share_target: float) -> list[tuple]:
    """A short connection_quality_changed sequence. Quality holds until the next
    event, so a single poor/lost window of length share_target*duration reproduces
    the intended poor|lost second-share."""
    rows = [
        (start.strftime(_TS_FMT), room_sid, account_id, agent, "excellent"),
        (start.strftime(_TS_FMT), room_sid, account_id, caller, "excellent"),
    ]
    dur = (end - start).total_seconds()
    if share_target > 0.01 and dur > 20:
        bad_len = _clamp(share_target * dur, 3, dur - 8)
        bad_start = start + timedelta(seconds=rng.uniform(5, max(6.0, dur - bad_len - 5)))
        bad_q = "lost" if rng.random() < 0.3 else "poor"
        rows.append((bad_start.strftime(_TS_FMT), room_sid, account_id, caller, bad_q))
        rows.append(
            ((bad_start + timedelta(seconds=bad_len)).strftime(_TS_FMT),
             room_sid, account_id, caller, "good")
        )
    return rows


def _gen_session(rng: random.Random, account_id: str, profile: dict,
                 start: datetime, this_week: bool) -> tuple[list, list, list]:
    room_sid = "SIM_" + uuid.uuid4().hex[:12]
    room_name = f"{account_id}-{start:%Y%m%d}-{rng.randint(100, 999)}"
    caller = f"acct-{account_id}__caller-{rng.randint(1, 999)}"
    agent = f"agent-SIM{rng.randint(1000, 9999)}"

    mps_lo, mps_hi = profile.get("minutes_per_session", [3, 9])
    dur_s = rng.uniform(mps_lo * 60, mps_hi * 60)
    end = start + timedelta(seconds=dur_s)
    ungraceful = rng.random() < profile.get("ungraceful_rate", 0.03)

    # ---- events ----
    ev: list[tuple] = [
        _event_row(start, "room_started", room_sid, room_name, account_id, None,
                   {"room": {"sid": room_sid, "name": room_name,
                             "metadata": json.dumps({"account_id": account_id})}}),
        _event_row(start + timedelta(seconds=1), "participant_joined", room_sid, room_name,
                   account_id, agent, {"participant": {"identity": agent}}),
        _event_row(start + timedelta(seconds=2), "participant_joined", room_sid, room_name,
                   account_id, caller, {"participant": {"identity": caller}}),
    ]
    if ungraceful:
        ev.append(_event_row(end, "participant_connection_aborted", room_sid, room_name,
                             account_id, caller,
                             {"participant": {"identity": caller,
                                              "disconnect_reason": "CONNECTION_TIMEOUT"}}))
    else:
        ev.append(_event_row(end, "participant_left", room_sid, room_name, account_id, caller,
                             {"participant": {"identity": caller,
                                              "disconnect_reason": "CLIENT_INITIATED"}}))
    ev.append(_event_row(end + timedelta(seconds=1), "room_finished", room_sid, room_name,
                         account_id, None, {"room": {"sid": room_sid, "name": room_name}}))

    # ---- turn_metrics ----
    lat = profile["latency_ms"]
    mean, sd = lat["mean"], lat["sd"]
    ttft_mult = profile.get("this_week_ttft_mult", 1.0) if this_week else 1.0
    err_rate = profile.get("this_week_error_rate", profile.get("error_rate", 0.01)) if this_week \
        else profile.get("error_rate", 0.01)

    n_turns = rng.randint(3, 8)
    turns: list[tuple] = []
    for ti in range(n_turns):
        t_at = start + timedelta(seconds=3 + ti * (dur_s / (n_turns + 1)))
        # total_latency_ms is drawn straight from the profile (drives amber/red).
        # The component split is illustrative; this_week_ttft_mult inflates only the
        # stored ttft_ms so the p95-regression signal can fire without dragging total
        # into amber. (Real agent rows store total = eou+transcription+ttft+ttfb;
        # for synthetic rows total is the primary draw - see DESIGN.md.)
        total = _clamp(rng.gauss(mean, sd), 250, 8000)
        eou = _clamp(rng.gauss(0.34 * total, 0.05 * total), 50, 0.6 * total)
        ttfb = _clamp(rng.gauss(0.12 * total, 0.03 * total), 30, 0.4 * total)
        base_ttft = 0.39 * total + rng.gauss(0, 0.05 * total)
        ttft = _clamp(base_ttft * ttft_mult, 60, 6000)
        turns.append((
            t_at.strftime(_TS_FMT), room_sid, account_id, "sim_" + uuid.uuid4().hex[:12],
            round(ttft, 1), round(ttfb, 1), round(eou, 1),
            round(total, 1),
            int(_clamp(rng.gauss(150, 40), 20, 600)),
            int(_clamp(rng.gauss(55, 15), 5, 200)),
            1 if rng.random() < err_rate else 0,
        ))

    # ---- quality_events ----
    pls = profile["poor_lost_share"]
    share_target = _clamp(rng.gauss(pls["mean"], pls["sd"]), 0.0, 0.8)
    qual = _gen_quality(rng, room_sid, account_id, caller, agent, start, end, share_target)
    return ev, turns, qual


def simulate(seed: int, days: int, reset: bool) -> None:
    accounts = load_accounts()
    rng = random.Random(seed)
    conn = connect()
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    managed = [a["account_id"] for a in accounts]

    if reset:
        marks = ",".join("?" * len(managed))
        for tbl in ("turn_metrics", "quality_events", "events", "sessions"):
            conn.execute(
                f"DELETE FROM {tbl} WHERE account_id IN ({marks}) "
                f"AND (room_sid IS NULL OR substr(room_sid, 1, 4) = 'SIM_')",
                managed,
            )
        conn.execute(f"DELETE FROM accounts WHERE account_id IN ({marks})", managed)
        conn.commit()

    grand = {"events": 0, "turn_metrics": 0, "quality_events": 0, "sessions": 0}
    for acc in accounts:
        conn.execute(
            "INSERT OR REPLACE INTO accounts (account_id, name, plan_minutes_limit, plan_concurrency_limit) "
            "VALUES (?, ?, ?, ?)",
            (acc["account_id"], acc["name"], acc.get("plan_minutes_limit"),
             acc.get("plan_concurrency_limit")),
        )
        profile = acc["profile"]
        ev_all: list[tuple] = []
        turn_all: list[tuple] = []
        qual_all: list[tuple] = []
        n_sessions = 0
        for day_offset in range(days, 0, -1):  # oldest -> 1 day ago (never "today")
            day = now - timedelta(days=day_offset)
            this_week = day_offset <= 7
            spd = profile.get("this_week_sessions_per_day") if this_week and "this_week_sessions_per_day" in profile \
                else profile.get("sessions_per_day", [4, 8])
            for _ in range(rng.randint(spd[0], spd[1])):
                start = day.replace(hour=rng.randint(7, 21), minute=rng.randint(0, 59),
                                    second=rng.randint(0, 59))
                ev, turns, qual = _gen_session(rng, acc["account_id"], profile, start, this_week)
                ev_all += ev
                turn_all += turns
                qual_all += qual
                n_sessions += 1

        conn.executemany(
            "INSERT INTO events (ts, type, room_sid, room_name, account_id, participant_id, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ev_all,
        )
        conn.executemany(
            "INSERT INTO turn_metrics (ts, room_sid, account_id, speech_id, ttft_ms, ttfb_ms, "
            "eou_ms, total_latency_ms, llm_tokens_in, llm_tokens_out, error_flag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            turn_all,
        )
        conn.executemany(
            "INSERT INTO quality_events (ts, room_sid, account_id, participant_id, quality) "
            "VALUES (?, ?, ?, ?, ?)",
            qual_all,
        )
        conn.commit()
        grand["events"] += len(ev_all)
        grand["turn_metrics"] += len(turn_all)
        grand["quality_events"] += len(qual_all)
        grand["sessions"] += n_sessions
        print(f"  {acc['account_id']:<12} {n_sessions:>4} sessions  "
              f"{len(turn_all):>5} turns  {len(qual_all):>5} quality  ({profile['kind']})")

    conn.close()
    print(f"\ntotal: {grand['sessions']} sessions, {grand['turn_metrics']} turn_metrics, "
          f"{grand['quality_events']} quality_events, {grand['events']} events "
          f"(seed={seed}, days={days}, reset={reset})")
    print("next: python -m pipeline.rollups")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--days", type=int, default=14, help="backdated window (need >=14 for a full prior week)")
    ap.add_argument("--reset", action=argparse.BooleanOptionalAction, default=True,
                    help="rebuild this script's rows (default) vs append")
    args = ap.parse_args()
    print(f"simulating {args.days}d of telemetry for accounts in {ACCOUNTS_YAML.name} ...")
    simulate(args.seed, args.days, args.reset)


if __name__ == "__main__":
    main()
