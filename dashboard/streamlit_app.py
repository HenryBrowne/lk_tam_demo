"""Portfolio Signal - Streamlit dashboard.

    streamlit run dashboard/streamlit_app.py

Portfolio overview (all accounts, verdict, trend, top risk, usage vs plan) plus a
per-account drill-down (session timeline, latency distributions, connection-quality
trend, turn detail, token cost). Every signal is shown next to the TAM action it
implies. Reads data/portfolio.db - run scripts/simulate_accounts.py first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))  # `streamlit run` puts dashboard/ on the path, not the repo root

from pipeline.db import connect, default_db_path  # noqa: E402
from pipeline.rollups import account_rollup, data_now, load_frames, scored_sessions  # noqa: E402
from pipeline.sessions import derive_sessions  # noqa: E402
from pipeline.thresholds import ILLUSTRATIVE_BANNER, load_thresholds  # noqa: E402

# --- palette: reserved status colours, always shown with a text label ---------
VERDICT_COLOR = {
    "Healthy": "#1a7f37", "Watch": "#b26a00", "At-risk": "#cf222e",
    "green": "#1a7f37", "amber": "#b26a00", "red": "#cf222e",
}
VERDICT_ICON = {"Healthy": "●", "Watch": "▲", "At-risk": "■"}
QUALITY_COLOR = {
    "excellent": "#1a7f37", "good": "#5aa469",
    "poor": "#b26a00", "lost": "#cf222e", "unknown": "#8b949e",
}
ACCENT, MUTED = "#2f6feb", "#9aa4b2"  # this-week vs prior-week

# --- signal -> what a TAM does about it --------------------------------------
SIGNAL_ACTION = {
    "sessions": "Proactive architecture review - session config is drifting off what scales.",
    "ttft_p95": "Proactive architecture review - time-to-first-token is trending up.",
    "quality": "Design consultation - customer edge / region / codec config.",
    "ungraceful": "Lead the escalation - something broke.",
    "error_rate": "Route to support / eng and keep customer leadership informed.",
    "usage": "Hand the expansion signal to sales.",
}
SIGNAL_TO_ACTION_TABLE = pd.DataFrame(
    [
        ["Turn latency / TTFT trending up", "Config drifting off what scales", "Proactive architecture review"],
        ["Connection-quality poor/lost ratio rising", "Customer edge / region / codec config", "Design consultation"],
        ["Ungraceful-disconnect rate spike", "Something broke", "Lead the escalation"],
        ["Session-minutes / concurrency vs plan, rising", "Growth", "Hand expansion signal to sales"],
        ["Error rate vs 7-day baseline", "Regression", "Route to support/eng, keep leadership informed"],
    ],
    columns=["Signal", "Meaning", "TAM action"],
)

# Illustrative unit economics for the token-cost panel: Claude Haiku 4.5 list price.
COST_IN_PER_MTOK, COST_OUT_PER_MTOK = 1.00, 5.00


# ---------------------------------------------------------------------------
# data loading (cached on the db file's mtime so "Refresh" actually refreshes)
# ---------------------------------------------------------------------------

def _db_mtime() -> float:
    p = Path(default_db_path())
    return p.stat().st_mtime if p.exists() else 0.0


@st.cache_data(show_spinner=False)
def load_all(_mtime: float):
    conn = connect()
    derive_sessions(conn)
    rollups = account_rollup(conn)
    scored = scored_sessions(conn)
    _, turns, quality, accounts = load_frames(conn)
    now = data_now(scored, turns, quality)
    conn.close()
    return rollups, scored, turns, quality, accounts, now


# ---------------------------------------------------------------------------
# small render helpers
# ---------------------------------------------------------------------------

def verdict_badge(v: str) -> str:
    return f":{'green' if v == 'Healthy' else 'orange' if v == 'Watch' else 'red'}[{VERDICT_ICON[v]} {v}]"


def pct(x, dash="-"):
    return dash if x is None or pd.isna(x) else f"{x:.0%}"


def threshold_lines(fig: go.Figure, values: list[float], labels: list[str]) -> None:
    for val, lab in zip(values, labels):
        fig.add_vline(x=val, line_dash="dash", line_color="#8b949e",
                      annotation_text=lab, annotation_position="top")


def base_layout(fig: go.Figure, height: int = 320, ytitle: str = "", xtitle: str = "") -> go.Figure:
    fig.update_layout(
        template="plotly_white", height=height, margin=dict(l=10, r=10, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0),
        yaxis_title=ytitle, xaxis_title=xtitle,
    )
    return fig


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------

def portfolio_view(rollups, scored, now, window_days):
    st.subheader("Portfolio overview")
    at_risk = [r for r in rollups if r.verdict == "At-risk"]
    watch = [r for r in rollups if r.verdict == "Watch"]
    this_lo = now - pd.Timedelta(days=window_days)
    this_week = scored[scored["started_at"] > this_lo]

    c = st.columns(5)
    c[0].metric("Accounts", len(rollups))
    c[1].metric("At-risk", len(at_risk))
    c[2].metric("Watch", len(watch))
    c[3].metric("Sessions (7d)", len(this_week))
    c[4].metric("Session-minutes (7d)", f"{this_week['duration_s'].fillna(0).sum() / 60:,.0f}")

    rows = []
    for r in rollups:
        rows.append(
            {
                "Account": r.name,
                "Verdict": f"{VERDICT_ICON[r.verdict]} {r.verdict}",
                "Trend": {"up": "↑ worse", "down": "↓ better", "flat": "→ flat"}[r.trend],
                "Top reason": r.top_reason,
                "Sess 7d": r.sessions_this,
                "Red/amber": pct(r.red_amber_share_this),
                "TTFT WoW": _wow(r.ttft_p95_this_ms, r.ttft_p95_prev_ms),
                "Err vs base": _err(r.error_rate_this, r.error_rate_prev),
                "Usage min": r.usage_minutes_pct if r.usage_minutes_pct is not None else 0.0,
                "Usage conc": r.usage_concurrency_pct if r.usage_concurrency_pct is not None else 0.0,
            }
        )
    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        column_config={
            "Top reason": st.column_config.TextColumn(width="large"),
            "Usage min": st.column_config.ProgressColumn(
                "Usage (min)", format="percent", min_value=0.0, max_value=1.2,
                help="session-minutes this week / plan_minutes_limit",
            ),
            "Usage conc": st.column_config.ProgressColumn(
                "Usage (conc)", format="percent", min_value=0.0, max_value=1.2,
                help="peak concurrent sessions this week / plan_concurrency_limit",
            ),
        },
    )
    st.caption("Pick an account in the sidebar to drill down. Numeric columns scroll right.")

    with st.expander("How to read this - signal → TAM action"):
        st.dataframe(SIGNAL_TO_ACTION_TABLE, hide_index=True, width="stretch")


def account_view(acc_id, rollups, scored, turns, quality, now, window_days):
    roll = next((r for r in rollups if r.account_id == acc_id), None)
    if roll is None:
        st.warning(f"No rollup for {acc_id}.")
        return

    st.subheader(f"{roll.name}  {verdict_badge(roll.verdict)}")
    st.caption(f"Top reason: {roll.top_reason}")

    this_lo = now - pd.Timedelta(days=window_days)
    a_sessions = scored[scored["account_id"] == acc_id]
    a_turns = turns[turns["account_id"] == acc_id]
    a_quality = quality[quality["account_id"] == acc_id]
    this_turns = a_turns[a_turns["ts"] > this_lo]

    # --- signal cards -> TAM action ---
    st.markdown("**Signals**")
    if roll.signals:
        for s in roll.signals:
            with st.container(border=True):
                st.markdown(f"{verdict_badge(s.level)} &nbsp; {s.detail}")
                st.caption(SIGNAL_ACTION.get(s.name, ""))
    else:
        st.success("All signals within thresholds.")

    tabs = st.tabs(["Session timeline", "Latency", "Connection quality", "Turn detail", "Token cost"])

    # --- 1. session timeline ---
    with tabs[0]:
        recent = a_sessions[a_sessions["started_at"] > now - pd.Timedelta(days=2 * window_days)].copy()
        if recent.empty:
            st.info("No sessions in the last two weeks.")
        else:
            recent["verdict_label"] = recent["verdict"].map(
                {"green": "green (ok)", "amber": "amber", "red": "red"}
            )
            fig = go.Figure()
            for v in ["green", "amber", "red"]:
                d = recent[recent["verdict"] == v]
                if d.empty:
                    continue
                fig.add_trace(
                    go.Scatter(
                        x=d["started_at"], y=d["median_latency_ms"], mode="markers",
                        name={"green": "ok", "amber": "amber", "red": "red"}[v],
                        marker=dict(color=VERDICT_COLOR[v], size=9, line=dict(width=1, color="white")),
                        customdata=d[["room_sid", "duration_s", "poor_lost_share", "n_participants"]],
                        hovertemplate=(
                            "%{x|%b %d %H:%M}<br>median latency %{y:.0f} ms<br>"
                            "room %{customdata[0]}<br>%{customdata[1]:.0f}s, "
                            "%{customdata[3]} participant(s)<br>"
                            "poor/lost %{customdata[2]:.0%}<extra></extra>"
                        ),
                    )
                )
            fig.add_hline(y=900, line_dash="dash", line_color="#8b949e",
                          annotation_text="amber ≥ 900ms", annotation_position="right")
            fig.add_hline(y=1800, line_dash="dash", line_color="#cf222e",
                          annotation_text="red ≥ 1800ms", annotation_position="right")
            base_layout(fig, ytitle="median turn latency (ms)")
            st.plotly_chart(fig, width="stretch")
            st.caption("One dot per session, coloured by session verdict (worst of latency / quality / stability).")

    # --- 2. latency distribution, this week vs prior week ---
    with tabs[1]:
        prev_turns = a_turns[(a_turns["ts"] > now - pd.Timedelta(days=2 * window_days)) & (a_turns["ts"] <= this_lo)]
        if this_turns.empty and prev_turns.empty:
            st.info("No turn metrics for this account.")
        else:
            fig = go.Figure()
            fig.add_trace(go.Histogram(x=prev_turns["total_latency_ms"], name="prior 7d",
                                       marker_color=MUTED, opacity=0.55, nbinsx=40))
            fig.add_trace(go.Histogram(x=this_turns["total_latency_ms"], name="this 7d",
                                       marker_color=ACCENT, opacity=0.75, nbinsx=40))
            fig.update_layout(barmode="overlay")
            threshold_lines(fig, [900, 1800], ["amber", "red"])
            base_layout(fig, ytitle="turns", xtitle="total latency per turn (ms)")
            st.plotly_chart(fig, width="stretch")

            comp = this_turns[["ttft_ms", "ttfb_ms", "eou_ms"]].melt(var_name="component", value_name="ms").dropna()
            if not comp.empty:
                bfig = go.Figure()
                for name in ["ttft_ms", "ttfb_ms", "eou_ms"]:
                    d = comp[comp["component"] == name]
                    bfig.add_trace(go.Box(y=d["ms"], name=name.replace("_ms", ""), marker_color=ACCENT,
                                          boxpoints=False))
                base_layout(bfig, height=260, ytitle="ms (this 7d)")
                bfig.update_layout(showlegend=False)
                st.plotly_chart(bfig, width="stretch")
                st.caption("TTFT = LLM time-to-first-token · TTFB = TTS time-to-first-byte · EOU = end-of-utterance delay.")

    # --- 3. connection quality: seconds at each level, this vs prior week ---
    with tabs[2]:
        if a_quality.empty:
            st.info("No connection-quality events for this account.")
        else:
            secs = _quality_seconds(a_sessions, a_quality, now, window_days)
            if secs.empty:
                st.info("Not enough data to attribute quality seconds.")
            else:
                fig = go.Figure()
                for wk, color in [("prior 7d", MUTED), ("this 7d", ACCENT)]:
                    d = secs[secs["week"] == wk]
                    fig.add_trace(go.Bar(x=d["quality"], y=d["seconds"], name=wk,
                                         marker_color=[QUALITY_COLOR[q] for q in d["quality"]] if wk == "this 7d" else color))
                fig.update_layout(barmode="group")
                base_layout(fig, ytitle="participant-seconds at level")
                st.plotly_chart(fig, width="stretch")
                bad = a_quality[a_quality["quality"].isin(["poor", "lost"])].sort_values("ts", ascending=False)
                st.caption(f"{len(bad)} poor/lost events all-time for this account. Most recent:")
                st.dataframe(bad.head(15)[["ts", "room_sid", "participant_id", "quality"]],
                             hide_index=True, width="stretch")

    # --- 4. turn detail table ---
    with tabs[3]:
        cols = ["ts", "room_sid", "ttft_ms", "ttfb_ms", "eou_ms", "total_latency_ms",
                "llm_tokens_in", "llm_tokens_out", "error_flag"]
        recent_turns = a_turns.sort_values("ts", ascending=False)[cols].head(100)
        st.dataframe(recent_turns, hide_index=True, width="stretch")
        st.caption("Most recent 100 turns. error_flag = the LLM or TTS call was cancelled / errored.")

    # --- 5. token cost ---
    with tabs[4]:
        tin, tout = int(this_turns["llm_tokens_in"].fillna(0).sum()), int(this_turns["llm_tokens_out"].fillna(0).sum())
        cost = tin / 1e6 * COST_IN_PER_MTOK + tout / 1e6 * COST_OUT_PER_MTOK
        c = st.columns(3)
        c[0].metric("LLM tokens in (7d)", f"{tin:,}")
        c[1].metric("LLM tokens out (7d)", f"{tout:,}")
        c[2].metric("Est. LLM cost (7d)", f"${cost:,.2f}")
        st.caption(
            f"Illustrative, at Claude Haiku 4.5 list price "
            f"(${COST_IN_PER_MTOK:.2f}/${COST_OUT_PER_MTOK:.2f} per 1M in/out). "
            "Token counts are real for the agent-driven rows and synthetic for the seeded history."
        )
        if not this_turns.empty:
            daily = (
                this_turns.assign(day=this_turns["ts"].dt.date)
                .groupby("day")[["llm_tokens_in", "llm_tokens_out"]].sum().reset_index()
            )
            fig = go.Figure()
            fig.add_trace(go.Bar(x=daily["day"], y=daily["llm_tokens_in"], name="in", marker_color=MUTED))
            fig.add_trace(go.Bar(x=daily["day"], y=daily["llm_tokens_out"], name="out", marker_color=ACCENT))
            fig.update_layout(barmode="stack")
            base_layout(fig, height=260, ytitle="tokens / day")
            st.plotly_chart(fig, width="stretch")

    st.divider()
    st.info("Account brief - the auto-drafted exec summary is generated in Phase 5 and will appear here.")


# ---------------------------------------------------------------------------
# helpers that need session<->quality attribution
# ---------------------------------------------------------------------------

def _quality_seconds(a_sessions: pd.DataFrame, a_quality: pd.DataFrame, now, window_days) -> pd.DataFrame:
    """Participant-seconds spent at each quality level, this week vs prior week,
    as a step function between events clipped to each session's span."""
    spans = {
        r.room_sid: (r.started_at, r.ended_at)
        for r in a_sessions.itertuples(index=False)
        if pd.notna(r.started_at) and pd.notna(r.ended_at)
    }
    this_lo = now - pd.Timedelta(days=window_days)
    prev_lo = now - pd.Timedelta(days=2 * window_days)
    out: dict[tuple[str, str], float] = {}
    for room, q in a_quality.sort_values("ts").groupby("room_sid"):
        if room not in spans:
            continue
        start, end = spans[room]
        week = "this 7d" if start > this_lo else "prior 7d" if start > prev_lo else None
        if week is None:
            continue
        times = list(q["ts"]) + [end]
        labels = list(q["quality"])
        for i, lab in enumerate(labels):
            seg = (min(times[i + 1], end) - max(times[i], start)).total_seconds()
            if seg > 0:
                out[(week, lab)] = out.get((week, lab), 0.0) + seg
    rows = [{"week": w, "quality": ql, "seconds": s} for (w, ql), s in out.items()]
    df = pd.DataFrame(rows)
    if not df.empty:
        order = ["excellent", "good", "poor", "lost", "unknown"]
        df["quality"] = pd.Categorical(df["quality"], categories=order, ordered=True)
        df = df.sort_values(["week", "quality"])
    return df


def _wow(this_ms, prev_ms) -> str:
    if this_ms is None:
        return "-"
    if prev_ms is None or prev_ms <= 0:
        return "n/a"
    return f"{(this_ms - prev_ms) / prev_ms:+.0%}"


def _err(this, prev) -> str:
    if prev and prev > 0:
        return f"{this:.0%} ({this / prev:.1f}x)"
    return f"{this:.0%}"


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

def main() -> None:
    st.set_page_config(page_title="Portfolio Signal", page_icon="📡", layout="wide")
    st.title("📡 Portfolio Signal")
    st.caption(ILLUSTRATIVE_BANNER)

    if not Path(default_db_path()).exists():
        st.error("No data/portfolio.db yet. Run:  python scripts/simulate_accounts.py")
        st.stop()

    window_days = int(load_thresholds()["account"]["window_days"])
    rollups, scored, turns, quality, accounts, now = load_all(_db_mtime())

    if not rollups:
        st.warning("No accounts scored yet. Run:  python scripts/simulate_accounts.py")
        st.stop()

    with st.sidebar:
        st.header("View")
        if st.button("↻ Refresh data", width="stretch"):
            st.cache_data.clear()
            st.rerun()
        options = ["Portfolio overview"] + [f"{r.name}" for r in rollups]
        choice = st.radio("Account", options, label_visibility="collapsed")
        st.caption(f"as of {now:%Y-%m-%d %H:%M} · window {window_days}d vs prior {window_days}d")

    if choice == "Portfolio overview":
        portfolio_view(rollups, scored, now, window_days)
    else:
        acc_id = next(r.account_id for r in rollups if r.name == choice)
        account_view(acc_id, rollups, scored, turns, quality, now, window_days)


main()
