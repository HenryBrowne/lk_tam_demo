"""Account rollups + the Phase 3 checkpoint CLI.

Reads ``sessions`` / ``turn_metrics`` / ``quality_events`` / ``accounts``, scores
each session, rolls the sessions up per account into this-week-vs-prior-week
signals, applies ``scoring.account_verdict``, and prints a portfolio table.

    python -m pipeline.rollups

"now" is taken as the latest timestamp in the data (not the wall clock) so the
week windows line up with backdated synthetic data from simulate_accounts.py.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

import pandas as pd

from pipeline.db import connect
from pipeline.scoring import AccountWindowMetrics, account_verdict, session_verdict
from pipeline.sessions import derive_sessions
from pipeline.thresholds import ILLUSTRATIVE_BANNER, load_thresholds

_TS_FMT = "%Y-%m-%d %H:%M:%S"
_POOR = {"poor", "lost"}


# ---------------------------------------------------------------------------
# per-session metrics
# ---------------------------------------------------------------------------

def _poor_lost_share(quality_rows: pd.DataFrame, start: datetime, end: datetime) -> float:
    """Fraction of [start, end] spent at quality poor|lost, as a step function
    between connection_quality_changed events (quality holds until the next one)."""
    total = (end - start).total_seconds()
    if total <= 0 or quality_rows.empty:
        return 0.0
    q = quality_rows.sort_values("ts")
    times = list(q["ts"]) + [end]
    labels = list(q["quality"])
    bad = 0.0
    for i, label in enumerate(labels):
        seg_start = max(times[i], start)
        seg_end = min(times[i + 1], end)
        if seg_end > seg_start and label in _POOR:
            bad += (seg_end - seg_start).total_seconds()
    return min(bad / total, 1.0)


def _peak_concurrency(spans: list[tuple[datetime, datetime]]) -> int:
    """Max number of sessions overlapping at any instant (sweep line)."""
    if not spans:
        return 0
    points: list[tuple[datetime, int]] = []
    for s, e in spans:
        points.append((s, 1))
        points.append((e, -1))
    points.sort(key=lambda p: (p[0], p[1]))
    cur = peak = 0
    for _, delta in points:
        cur += delta
        peak = max(peak, cur)
    return peak


def _p95(series: pd.Series) -> float | None:
    s = series.dropna()
    return float(s.quantile(0.95)) if len(s) else None


# ---------------------------------------------------------------------------
# rollup
# ---------------------------------------------------------------------------

@dataclass
class AccountRollup:
    account_id: str
    name: str
    verdict: str
    trend: str
    top_reason: str
    sessions_this: int
    red_amber_share_this: float
    ttft_p95_this_ms: float | None
    ttft_p95_prev_ms: float | None
    error_rate_this: float
    error_rate_prev: float
    usage_minutes_pct: float | None
    usage_concurrency_pct: float | None


def account_rollup(conn: sqlite3.Connection) -> list[AccountRollup]:
    th = load_thresholds()
    sessions = pd.read_sql_query("SELECT * FROM sessions", conn)
    turns = pd.read_sql_query("SELECT * FROM turn_metrics", conn)
    quality = pd.read_sql_query("SELECT * FROM quality_events", conn)
    accounts = pd.read_sql_query("SELECT * FROM accounts", conn)

    if sessions.empty:
        return []

    for df, cols in [
        (sessions, ["started_at", "ended_at"]),
        (turns, ["ts"]),
        (quality, ["ts"]),
    ]:
        for c in cols:
            df[c] = pd.to_datetime(df[c], format=_TS_FMT, errors="coerce")

    # "now" and the two week windows
    now = max(
        [t for t in [sessions["ended_at"].max(), turns["ts"].max(), quality["ts"].max()] if pd.notna(t)]
    )
    wd = int(th["account"]["window_days"])
    this_lo, prev_lo = now - timedelta(days=wd), now - timedelta(days=2 * wd)

    plans = accounts.set_index("account_id").to_dict("index")

    # score every session
    srows = []
    for s in sessions.itertuples(index=False):
        room_turns = turns[turns["room_sid"] == s.room_sid]
        room_q = quality[quality["room_sid"] == s.room_sid]
        median_lat = (
            float(room_turns["total_latency_ms"].median())
            if not room_turns["total_latency_ms"].dropna().empty
            else None
        )
        start = s.started_at if pd.notna(s.started_at) else None
        end = s.ended_at if pd.notna(s.ended_at) else None
        share = _poor_lost_share(room_q, start, end) if (start and end) else 0.0
        sv = session_verdict(median_lat, share, s.graceful_end, s.duration_s, th)
        srows.append(
            {
                "account_id": s.account_id,
                "room_sid": s.room_sid,
                "started_at": start,
                "ended_at": end,
                "duration_s": s.duration_s,
                "graceful_end": s.graceful_end,
                "verdict": sv.verdict,
            }
        )
    scored = pd.DataFrame(srows)

    out: list[AccountRollup] = []
    account_ids = sorted(set(scored["account_id"].dropna()) | set(accounts["account_id"]))
    for acc in account_ids:
        a_sessions = scored[scored["account_id"] == acc]
        a_turns = turns[turns["account_id"] == acc]
        this_s = a_sessions[a_sessions["started_at"] > this_lo]
        prev_s = a_sessions[(a_sessions["started_at"] > prev_lo) & (a_sessions["started_at"] <= this_lo)]
        this_t = a_turns[a_turns["ts"] > this_lo]
        prev_t = a_turns[(a_turns["ts"] > prev_lo) & (a_turns["ts"] <= this_lo)]

        def ra_share(df: pd.DataFrame) -> float:
            return 0.0 if df.empty else float(df["verdict"].isin(["red", "amber"]).mean())

        def ung_share(df: pd.DataFrame) -> float:
            return 0.0 if df.empty else float((df["graceful_end"] == 0).mean())

        plan = plans.get(acc, {})
        min_lim = plan.get("plan_minutes_limit")
        conc_lim = plan.get("plan_concurrency_limit")

        def minutes(df: pd.DataFrame) -> float:
            return float(df["duration_s"].fillna(0).sum()) / 60.0

        usage_min_this = (minutes(this_s) / min_lim) if min_lim else None
        usage_min_prev = (minutes(prev_s) / min_lim) if min_lim else None
        peak = _peak_concurrency(
            [
                (r.started_at, r.ended_at)
                for r in this_s.itertuples(index=False)
                if pd.notna(r.started_at) and pd.notna(r.ended_at)
            ]
        )
        usage_conc_this = (peak / conc_lim) if conc_lim else None

        m = AccountWindowMetrics(
            account_id=acc,
            name=plan.get("name", acc),
            sessions_this=len(this_s),
            sessions_prev=len(prev_s),
            red_amber_share_this=ra_share(this_s),
            red_amber_share_prev=ra_share(prev_s),
            ungraceful_share_this=ung_share(this_s),
            ttft_p95_this_ms=_p95(this_t["ttft_ms"]) if not this_t.empty else None,
            ttft_p95_prev_ms=_p95(prev_t["ttft_ms"]) if not prev_t.empty else None,
            error_rate_this=float(this_t["error_flag"].mean()) if not this_t.empty else 0.0,
            error_rate_prev=float(prev_t["error_flag"].mean()) if not prev_t.empty else 0.0,
            usage_minutes_pct=usage_min_this,
            usage_minutes_pct_prev=usage_min_prev,
            usage_concurrency_pct=usage_conc_this,
        )
        score = account_verdict(m, th)
        out.append(
            AccountRollup(
                account_id=acc,
                name=m.name,
                verdict=score.verdict,
                trend=score.trend,
                top_reason=score.top_reason,
                sessions_this=m.sessions_this,
                red_amber_share_this=m.red_amber_share_this,
                ttft_p95_this_ms=m.ttft_p95_this_ms,
                ttft_p95_prev_ms=m.ttft_p95_prev_ms,
                error_rate_this=m.error_rate_this,
                error_rate_prev=m.error_rate_prev,
                usage_minutes_pct=m.usage_minutes_pct,
                usage_concurrency_pct=m.usage_concurrency_pct,
            )
        )

    order = {"At-risk": 0, "Watch": 1, "Healthy": 2}
    out.sort(key=lambda r: (order.get(r.verdict, 3), r.account_id))
    return out


# ---------------------------------------------------------------------------
# CLI table
# ---------------------------------------------------------------------------

_ARROW = {"up": "↑", "down": "↓", "flat": "→"}


def _fmt_pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def _fmt_wow(this_ms: float | None, prev_ms: float | None) -> str:
    if this_ms is None:
        return "-"
    if prev_ms is None or prev_ms <= 0:
        return f"{this_ms / 1000:.1f}s (no WoW)"
    return f"{this_ms / 1000:.1f}s ({(this_ms - prev_ms) / prev_ms:+.0%})"


def _fmt_err(this: float, prev: float) -> str:
    if prev > 0:
        return f"{this:.0%} ({this / prev:.1f}x)"
    return f"{this:.0%} (base 0)"


def format_portfolio(rollups: list[AccountRollup]) -> str:
    header = ["ACCOUNT", "VERDICT", "TREND", "SESS", "RED/AMBER", "p95 TTFT (WoW)", "ERR vs base", "USAGE min/conc", "TOP REASON"]
    rows = [header]
    for r in rollups:
        rows.append(
            [
                r.name,
                r.verdict,
                _ARROW.get(r.trend, r.trend),
                str(r.sessions_this),
                _fmt_pct(r.red_amber_share_this),
                _fmt_wow(r.ttft_p95_this_ms, r.ttft_p95_prev_ms),
                _fmt_err(r.error_rate_this, r.error_rate_prev),
                f"{_fmt_pct(r.usage_minutes_pct)} / {_fmt_pct(r.usage_concurrency_pct)}",
                r.top_reason,
            ]
        )
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = []
    for j, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
        if j == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # trend arrows on Windows consoles
    except Exception:
        pass
    conn = connect()
    n_sessions = derive_sessions(conn)
    rollups = account_rollup(conn)
    conn.close()

    print("PORTFOLIO SIGNAL - portfolio overview")
    print(ILLUSTRATIVE_BANNER)
    print(f"({n_sessions} sessions across {len(rollups)} accounts; window = trailing 7d vs prior 7d)\n")
    if not rollups:
        print("no data yet - run: python scripts/simulate_accounts.py")
        return 0
    print(format_portfolio(rollups))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
