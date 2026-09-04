"""Auto-drafted account brief: the account's scored numbers -> a tight Claude
call -> a 6-8 sentence exec summary.

The deterministic pipeline (pipeline/scoring.py) makes the judgement. This only
turns the resulting numbers into prose - the prompt (brief/prompt.md) forbids the
model from inventing metrics or re-scoring. Model: BRIEF_LLM_MODEL (default
claude-sonnet-5) via the anthropic SDK directly.

    python -m brief.llm_brief --account acme-corp
    python -m brief.llm_brief --account globex --facts-only   # no API call
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from pipeline.db import connect
from pipeline.rollups import account_rollup, data_now, load_frames, scored_sessions
from pipeline.sessions import derive_sessions
from pipeline.thresholds import load_thresholds

load_dotenv()

_PROMPT_PATH = Path(__file__).with_name("prompt.md")
DEFAULT_MODEL = os.environ.get("BRIEF_LLM_MODEL", "claude-sonnet-5")


class BriefError(RuntimeError):
    """A brief could not be produced (unknown account, missing key, API failure)."""

# Illustrative unit economics, matching the dashboard (Claude Haiku 4.5 list price).
_COST_IN, _COST_OUT = 1.00, 5.00

_SIGNAL_ACTION = {
    "sessions": "proactive architecture review (session config drifting off what scales)",
    "ttft_p95": "proactive architecture review (time-to-first-token trending up)",
    "quality": "design consultation (customer edge / region / codec config)",
    "ungraceful": "lead the escalation (something broke)",
    "error_rate": "route to support/eng, keep customer leadership informed",
    "usage": "hand the expansion signal to sales",
}


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None or pd.isna(x) else f"{x:.0%}"


def _fmt_s(ms: float | None) -> str:
    return "n/a" if ms is None or pd.isna(ms) else f"{ms / 1000:.1f}s"


def account_facts(conn: sqlite3.Connection, account_id: str) -> str:
    """Build the compact DATA block the prompt consumes. All numbers come from the
    same rollup the dashboard shows."""
    th = load_thresholds()
    window_days = int(th["account"]["window_days"])

    rollups = account_rollup(conn)
    roll = next((r for r in rollups if r.account_id == account_id), None)
    if roll is None:
        raise BriefError(f"no account '{account_id}' - known: {[r.account_id for r in rollups]}")

    scored = scored_sessions(conn)
    _, turns, quality, _ = load_frames(conn)
    now = data_now(scored, turns, quality)
    this_lo = now - pd.Timedelta(days=window_days)

    s = scored[(scored["account_id"] == account_id) & (scored["started_at"] > this_lo)]
    t = turns[(turns["account_id"] == account_id) & (turns["ts"] > this_lo)]

    n_live = int((s["source"] == "live").sum())
    median_sess_latency = s["median_latency_ms"].median() if not s.empty else None
    poor_lost = s["poor_lost_share"].mean() if not s.empty else 0.0
    ungraceful = (s["graceful_end"] == 0).mean() if not s.empty else 0.0
    tin, tout = int(t["llm_tokens_in"].fillna(0).sum()), int(t["llm_tokens_out"].fillna(0).sum())
    cost = tin / 1e6 * _COST_IN + tout / 1e6 * _COST_OUT

    ttft_delta = (
        f" (+{(roll.ttft_p95_this_ms - roll.ttft_p95_prev_ms) / roll.ttft_p95_prev_ms:.0%} WoW)"
        if roll.ttft_p95_this_ms and roll.ttft_p95_prev_ms
        else ""
    )
    trend = {"up": "worsening", "down": "improving", "flat": "steady"}[roll.trend]

    # only the signals that actually pushed the verdict above Healthy
    driving = [sig for sig in (roll.signals or []) if sig.level != "Healthy"]
    fired = (
        "\n".join(f"  - {sig.name} [{sig.level}]: {sig.detail}" for sig in driving)
        if driving
        else "  - none (all signals within thresholds; verdict is Healthy)"
    )
    actions = "\n".join(f"  - {name}: {act}" for name, act in _SIGNAL_ACTION.items())

    return "\n".join(
        [
            f"account: {roll.name} ({account_id})",
            f"health verdict: {roll.verdict}",
            f"week-over-week trend: {trend} "
            f"(red/amber session share {_fmt_pct(roll.red_amber_share_this)} this week)",
            f"top reason: {roll.top_reason}",
            "",
            f"this week (trailing {window_days}d):",
            f"  sessions: {len(s)}  ({n_live} from real live LiveKit calls, {len(s) - n_live} simulated)",
            f"  red/amber session share: {_fmt_pct(roll.red_amber_share_this)}",
            f"  median session latency (p50): {_fmt_s(median_sess_latency)}",
            f"  p95 time-to-first-token: {_fmt_s(roll.ttft_p95_this_ms)}{ttft_delta}"
            f"  (prior week {_fmt_s(roll.ttft_p95_prev_ms)})",
            f"  error rate: {_fmt_pct(roll.error_rate_this)}  (prior-week baseline {_fmt_pct(roll.error_rate_prev)})",
            f"  ungraceful-disconnect share: {_fmt_pct(ungraceful)}",
            f"  connection-quality poor|lost share: {_fmt_pct(poor_lost)}",
            f"  usage: session-minutes {_fmt_pct(roll.usage_minutes_pct)} of plan limit; "
            f"peak concurrency {_fmt_pct(roll.usage_concurrency_pct)} of plan limit",
            f"  LLM token cost (illustrative, {tin:,} in / {tout:,} out): ${cost:,.2f}",
            "",
            "fired signals (verdict-level):",
            fired,
            "",
            "signal -> TAM action reference:",
            actions,
        ]
    )


def generate_brief(facts: str, model: str | None = None) -> str:
    """One Claude call. Raises SystemExit with a readable message on failure."""
    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise BriefError("ANTHROPIC_API_KEY is not set (see .env.example).")

    system = _PROMPT_PATH.read_text(encoding="utf-8").strip()
    user = f"Account data:\n\n{facts}"

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model or DEFAULT_MODEL,
            max_tokens=700,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as exc:  # auth / rate limit / bad request / server
        raise BriefError(f"brief generation failed: {type(exc).__name__}: {exc}") from exc

    return "".join(b.text for b in resp.content if b.type == "text").strip()


def brief_for_account(conn: sqlite3.Connection, account_id: str, model: str | None = None) -> str:
    derive_sessions(conn)
    return generate_brief(account_facts(conn, account_id), model)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", required=True)
    ap.add_argument("--model", default=None, help=f"override BRIEF_LLM_MODEL (default {DEFAULT_MODEL})")
    ap.add_argument("--facts-only", action="store_true", help="print the DATA block, make no API call")
    args = ap.parse_args()

    conn = connect()
    derive_sessions(conn)
    try:
        facts = account_facts(conn, args.account)
        if args.facts_only:
            print(facts)
            return
        print(generate_brief(facts, args.model))
    except BriefError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
