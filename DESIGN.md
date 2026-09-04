# DESIGN — Portfolio Signal

Scoring rationale and the verified-vs-inferred split. The prose write-up (README) is
finished in Phase 6; the scoring design below is current as of Phase 3.

## Why deterministic scoring (not a model)

Account health is scored with plain rules and thresholds in
`pipeline/thresholds.yaml`, not a learned model, because a TAM needs to be able to:

- **Audit** every verdict — "this account is At-risk *because* p95 TTFT rose 41%
  week-over-week and 28% of sessions were red".
- **Tune** thresholds per customer — a consumer-app account and a contact-centre
  account don't have the same latency bar.
- **Co-own** the thresholds with the customer in a QBR — you can't co-own a
  gradient-boosted model.

## Thresholds are illustrative

The numbers in `thresholds.yaml` (latency 900/1800 ms, quality 5%/20%, etc.) are
starting points chosen to make the demo legible, not benchmarks from LiveKit. Every
threshold is labelled as illustrative in the file and in the UI. The deliverable is
the **framework** — signal -> verdict -> TAM action — which survives re-tuning.

## LiveKit facts I verified vs. reasoning by analogy

See `NOTES-livekit-api.md` for the detailed verified-vs-assumed split on the API
surface. At the *design* level:

- **Verified:** the metric objects exist and carry per-component latency
  (`LLMMetrics.ttft`, `TTSMetrics.ttfb`, `EOUMetrics.end_of_utterance_delay`); the
  webhook model; the connection-quality event and its `excellent/good/poor/lost`
  levels.
- **Reasoning by analogy from other platforms (BI / observability / SaaS health
  scoring):** that week-over-week trend + a baseline comparison is the right shape
  for a health signal; that a red/amber/green worst-of rollup is legible to execs;
  that usage-vs-plan-limit is the cleanest expansion signal. None of that is
  LiveKit-specific — it's carried over from dashboards I've built before.

## Derived quantities that are approximations

- **`total_latency_ms`** per turn. For a **real** agent row it is
  `eou + transcription_delay + llm.ttft + tts.ttfb` (assembled in
  `agent/metrics_sink.py`) — a stand-in for "user stopped talking -> first agent
  audio", not a figure LiveKit reports directly. For a **synthetic** row
  (`simulate_accounts.py`) it is the primary draw from the account's latency profile,
  and the per-component split (`ttft_ms`/`ttfb_ms`/`eou_ms`) is illustrative rather
  than summing to it.
- **Connection-quality time-at-level** = step function between change events, clipped
  to the session's `[started_at, ended_at]` (`pipeline/rollups.py::_poor_lost_share`).
  We only receive transitions, not a continuous sample, and quality before the first
  event is assumed not-poor.
- **Graceful vs ungraceful end** = inference from `participant_connection_aborted` or a
  `participant_left` whose `disconnect_reason` is not a clean client leave
  (`pipeline/sessions.py`), pending confirmation of which reason codes LiveKit actually
  sends on the webhook (`NOTES-livekit-api.md`). `graceful_end` is `NULL` when unknown.
- **Fallback sessions.** A room with `turn_metrics`/`quality_events` but no `events`
  (a real agent call before the webhook path is live) gets a synthesized `sessions`
  row: `started_at`/`ended_at` = min/max ts of those rows, `graceful_end = NULL`,
  `n_participants` from distinct non-agent `quality_events` identities. `duration_s`
  here is bounded by metric-flush timestamps, so it under-reads the true call length.
  All 6 real LiveKit sessions currently land here, so none of them carry a
  graceful/ungraceful verdict yet — that needs the webhook path.

## Live vs simulated rows (`source` column)

Every telemetry row (`events`, `turn_metrics`, `quality_events`, `sessions`) carries
`source`: `live` (a real LiveKit Cloud call — agent worker joined a real room, real
`metrics_collected` / `connection_quality_changed` captured) or `sim`
(`scripts/simulate_accounts.py`). The simulator exists only because there is one
real LiveKit test project, not a portfolio; the scoring framework is identical for
both. The dashboard surfaces the split so a reviewer can see exactly which numbers
are real. Real calls are deliberately mixed into seeded accounts (`acme-corp`,
`globex`, `northwind`) — with real telemetry folded in, `acme-corp`'s verdict moves
because its live calls run slower than its simulated history.
- **Peak concurrency** = max overlap of session `[start, end]` spans in the window
  (sweep line, `pipeline/rollups.py::_peak_concurrency`).

## Scoring rules (implemented in `pipeline/scoring.py`)

Pure functions, no I/O — they take numbers + the `thresholds.yaml` dict and return a
verdict plus a human reason. Every verdict is traceable to one number and one
threshold line.

### Session verdict (per room) = worst of three sub-verdicts

| sub-verdict | input | green / amber / red |
|---|---|---|
| latency | median `total_latency_ms` across the session's turns | `< 900` / `900–1800` / `≥ 1800` ms |
| quality | fraction of session-seconds at connection quality poor\|lost | `< 5%` / `5–20%` / `≥ 20%` |
| stability | `graceful_end` + `duration_s` | red if ungraceful **or** session `< 10 s`; else green (unknown counts as green) |

### Account verdict (trailing 7d vs prior 7d) = worst of the weekly signals

"Now" is the latest timestamp in the data, not the wall clock, so backdated synthetic
data still lines up with the windows.

| signal | Watch | At-risk | notes |
|---|---|---|---|
| red/amber session share (this week) | `> 18%` | `> 40%` | amber-heavy accounts land Watch; At-risk needs a lot of outright-red sessions |
| p95 turn TTFT, this week vs last | `≥ +15%` | `≥ +30%` | only scored when this-week p95 `≥ 250 ms` (ratio on tiny numbers is noise) |
| error rate vs prior-week baseline | `≥ 1.3×` | `≥ 2.0×` | only scored when this-week rate `≥ 3%` — a 2× jump on a 0.3% rate is not a regression |
| ungraceful-disconnect share (this week) | `> 10%` | `> 20%` | |
| usage vs plan limit (minutes or concurrency) | `≥ 75%`, or `≥ 90%` **and rising** → "expansion signal" | — | usage never by itself makes an account At-risk; it is a sales hand-off, surfaced as the reason |

**Combining:** account verdict = worst level across the signals that fired. `top_reason`
= the human string of the worst-and-highest-priority signal
(`ttft_p95 > sessions = ungraceful > error_rate > usage`); if nothing fired,
"all signals within thresholds". `trend` (↑ / ↓ / →) is the week-over-week direction of
the red/amber session share.

Every number above lives in `pipeline/thresholds.yaml` behind an ILLUSTRATIVE banner.
The absolute-floor guards (`min_abs`, `min_abs_ms`) and the "usage is never At-risk"
rule are judgement calls carried over from BI dashboards, not from LiveKit.
