"""Deterministic health scoring for Portfolio Signal.

Pure functions only - no DB, no I/O, no clock. They take numbers plus the
thresholds dict (from ``thresholds.py``) and return a verdict and a human reason.
That is the whole point of doing it this way: every verdict a TAM shows an exec
can be traced back to a specific number and a specific line in ``thresholds.yaml``,
and the customer can argue with the threshold instead of a black box.

Session verdict  = worst of {latency, quality, stability}.
Account verdict  = worst of the weekly signals {red/amber session share,
                   p95 TTFT regression, error rate vs baseline, ungraceful-
                   disconnect share, usage vs plan}.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SessionVerdict = Literal["green", "amber", "red"]
AccountVerdict = Literal["Healthy", "Watch", "At-risk"]

_SESSION_RANK = {"green": 0, "amber": 1, "red": 2}
_ACCOUNT_RANK = {"Healthy": 0, "Watch": 1, "At-risk": 2}
# a session verdict maps onto an account-level band when rolled up
_SESSION_TO_ACCOUNT: dict[SessionVerdict, AccountVerdict] = {
    "green": "Healthy",
    "amber": "Watch",
    "red": "At-risk",
}


def _worst_session(*vs: SessionVerdict) -> SessionVerdict:
    return max(vs, key=_SESSION_RANK.__getitem__)


def _worst_account(*vs: AccountVerdict) -> AccountVerdict:
    return max(vs, key=_ACCOUNT_RANK.__getitem__)


# ---------------------------------------------------------------------------
# Session level
# ---------------------------------------------------------------------------

def session_latency_verdict(median_total_latency_ms: float | None, th: dict) -> SessionVerdict:
    if median_total_latency_ms is None:  # no turns recorded -> nothing to flag
        return "green"
    band = th["session"]["latency_ms"]
    if median_total_latency_ms < band["green_below"]:
        return "green"
    if median_total_latency_ms < band["amber_below"]:
        return "amber"
    return "red"


def session_quality_verdict(poor_lost_share: float, th: dict) -> SessionVerdict:
    band = th["session"]["quality_share"]
    if poor_lost_share < band["green_below"]:
        return "green"
    if poor_lost_share < band["amber_below"]:
        return "amber"
    return "red"


def session_stability_verdict(
    graceful_end: int | None, duration_s: float | None, th: dict
) -> SessionVerdict:
    min_s = th["session"]["stability"]["min_session_seconds"]
    if graceful_end == 0:
        return "red"
    if duration_s is not None and duration_s < min_s:
        return "red"
    return "green"  # graceful, or unknown (None) and long enough


@dataclass
class SessionScore:
    verdict: SessionVerdict
    reason: str
    latency: SessionVerdict
    quality: SessionVerdict
    stability: SessionVerdict


def session_verdict(
    median_total_latency_ms: float | None,
    poor_lost_share: float,
    graceful_end: int | None,
    duration_s: float | None,
    th: dict,
) -> SessionScore:
    lat = session_latency_verdict(median_total_latency_ms, th)
    qual = session_quality_verdict(poor_lost_share, th)
    stab = session_stability_verdict(graceful_end, duration_s, th)
    worst = _worst_session(lat, qual, stab)

    drivers: list[str] = []
    if worst != "green":
        if lat == worst and median_total_latency_ms is not None:
            drivers.append(f"median latency {median_total_latency_ms:.0f}ms")
        if qual == worst:
            drivers.append(f"{poor_lost_share:.0%} of session at poor/lost")
        if stab == worst:
            drivers.append(
                "ungraceful end" if graceful_end == 0 else "session under 10s"
            )
    reason = "; ".join(drivers) if drivers else "within thresholds"
    return SessionScore(worst, reason, lat, qual, stab)


# ---------------------------------------------------------------------------
# Account level
# ---------------------------------------------------------------------------

@dataclass
class AccountWindowMetrics:
    account_id: str
    name: str
    sessions_this: int
    sessions_prev: int
    red_amber_share_this: float
    red_amber_share_prev: float
    ungraceful_share_this: float
    ttft_p95_this_ms: float | None
    ttft_p95_prev_ms: float | None
    error_rate_this: float
    error_rate_prev: float                 # the baseline
    usage_minutes_pct: float | None        # this-week minutes / plan_minutes_limit
    usage_minutes_pct_prev: float | None
    usage_concurrency_pct: float | None    # peak concurrency / plan_concurrency_limit


@dataclass
class Signal:
    name: str
    level: AccountVerdict
    detail: str
    priority: int  # tie-breaker for which signal becomes top_reason (higher = wins)


@dataclass
class AccountScore:
    verdict: AccountVerdict
    top_reason: str
    trend: str                              # "up" | "down" | "flat" (red/amber session share WoW)
    signals: list[Signal] = field(default_factory=list)


def _trend(this: float, prev: float, tol: float = 0.03) -> str:
    if this > prev + tol:
        return "up"
    if this < prev - tol:
        return "down"
    return "flat"


def _pct_delta(this: float | None, prev: float | None) -> float | None:
    if this is None or prev is None or prev <= 0:
        return None
    return (this - prev) / prev


def account_verdict(m: AccountWindowMetrics, th: dict) -> AccountScore:
    acc = th["account"]
    signals: list[Signal] = []

    # 1. red/amber session share this week
    ra = acc["red_amber_session_share"]
    if m.red_amber_share_this > ra["at_risk_above"]:
        lvl: AccountVerdict = "At-risk"
    elif m.red_amber_share_this > ra["watch_above"]:
        lvl = "Watch"
    else:
        lvl = "Healthy"
    arrow = {"up": "up from", "down": "down from", "flat": "vs"}[
        _trend(m.red_amber_share_this, m.red_amber_share_prev)
    ]
    signals.append(
        Signal(
            "sessions",
            lvl,
            f"{m.red_amber_share_this:.0%} of sessions red/amber ({arrow} {m.red_amber_share_prev:.0%})",
            priority=4,
        )
    )

    # 2. p95 TTFT regression this week vs last (only above an absolute floor)
    delta = _pct_delta(m.ttft_p95_this_ms, m.ttft_p95_prev_ms)
    reg = acc["ttft_p95_regression"]
    if delta is not None and (m.ttft_p95_this_ms or 0) >= reg.get("min_abs_ms", 0):
        if delta >= reg["at_risk_pct"]:
            lvl = "At-risk"
        elif delta >= reg["watch_pct"]:
            lvl = "Watch"
        else:
            lvl = "Healthy"
        if lvl != "Healthy":
            signals.append(
                Signal(
                    "ttft_p95",
                    lvl,
                    f"p95 TTFT {delta:+.0%} WoW "
                    f"({m.ttft_p95_prev_ms / 1000:.1f}s->{m.ttft_p95_this_ms / 1000:.1f}s)",
                    priority=5,
                )
            )

    # 3. error rate vs prior-week baseline (only once the absolute rate clears a floor)
    err = acc["error_rate_vs_baseline"]
    if m.error_rate_this >= err.get("min_abs", 0):
        if m.error_rate_prev > 0:
            mult = m.error_rate_this / m.error_rate_prev
            if mult >= err["at_risk_mult"]:
                lvl = "At-risk"
            elif mult >= err["watch_mult"]:
                lvl = "Watch"
            else:
                lvl = "Healthy"
            detail = f"error rate {m.error_rate_this:.1%} = {mult:.1f}x the {m.error_rate_prev:.1%} baseline"
        else:
            lvl = "Watch"
            detail = f"error rate {m.error_rate_this:.1%} (no prior-week baseline)"
        if lvl != "Healthy":
            signals.append(Signal("error_rate", lvl, detail, priority=3))

    # 4. ungraceful-disconnect share this week
    ung = acc["ungraceful_share"]
    if m.ungraceful_share_this > ung["at_risk_above"]:
        lvl = "At-risk"
    elif m.ungraceful_share_this > ung["watch_above"]:
        lvl = "Watch"
    else:
        lvl = "Healthy"
    if lvl != "Healthy":
        signals.append(
            Signal("ungraceful", lvl, f"{m.ungraceful_share_this:.0%} of sessions ended ungracefully", priority=4)
        )

    # 5. usage vs plan limit (minutes or concurrency, whichever is higher)
    usage = acc["usage_vs_plan"]
    pcts = [p for p in (m.usage_minutes_pct, m.usage_concurrency_pct) if p is not None]
    if pcts:
        top = max(pcts)
        rising = (
            m.usage_minutes_pct is not None
            and m.usage_minutes_pct_prev is not None
            and m.usage_minutes_pct > m.usage_minutes_pct_prev
        )
        if top >= usage["expansion_pct"] and rising:
            signals.append(
                Signal(
                    "usage",
                    "Watch",
                    f"usage {top:.0%} of plan and rising - expansion signal, hand to sales",
                    priority=2,
                )
            )
        elif top >= usage["warn_pct"]:
            signals.append(
                Signal("usage", "Watch", f"usage {top:.0%} of plan limit", priority=1)
            )

    verdict = _worst_account("Healthy", *(s.level for s in signals))
    culprits = [s for s in signals if s.level == verdict and verdict != "Healthy"]
    if culprits:
        top_reason = max(culprits, key=lambda s: s.priority).detail
    else:
        top_reason = "all signals within thresholds"

    return AccountScore(
        verdict=verdict,
        top_reason=top_reason,
        trend=_trend(m.red_amber_share_this, m.red_amber_share_prev),
        signals=signals,
    )
