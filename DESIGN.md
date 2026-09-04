# DESIGN — Portfolio Signal

Scoring rationale, the verified-vs-inferred split, and the approximations the numbers
rest on. Current as of Phase 6. The narrative write-up (what this is, what I learned,
what's next) is in `README.md`; the API-surface detail is in `NOTES-livekit-api.md`.

**Framing (repeated from the README because it matters here most):** I picked LiveKit
up over a few days for this piece. Nothing below is real-time-systems expertise. Every
place I reasoned by analogy instead of verifying is called out as such.

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

`NOTES-livekit-api.md` has the line-by-line API detail. The design-level split:

### LiveKit facts I verified (source read + confirmed against a real call)

- The agent emits `metrics_collected` on `AgentSession`; the per-component objects
  carry `LLMMetrics.ttft`, `TTSMetrics.ttfb`, `EOUMetrics.end_of_utterance_delay` /
  `transcription_delay`, all **in seconds**. Cross-checked against a real call:
  `claude-haiku-4-5` TTFT ~0.65 s, `claude-sonnet-4-6` ~1.1 s on the same pipeline.
- `EOUMetrics` / `LLMMetrics` / `TTSMetrics` for one turn share a `speech_id` — that
  is the correlation key the turn-assembly in `agent/metrics_sink.py` relies on.
- Connection quality arrives as `room.on("connection_quality_changed")` →
  `(participant, quality)` with `QUALITY_{EXCELLENT,GOOD,POOR,LOST,UNKNOWN}`. Only
  *transitions* are delivered — there is no continuous sample. The agent receives
  these for the **remote** participant, not only itself.
- Webhook auth: a JWT in the `Authorization` header whose claims include a
  `sha256` of the body; verify with `livekit.api.WebhookReceiver` over the **raw**
  body bytes. Event names (`room_started/finished`,
  `participant_joined/left/connection_aborted`, `track_*`, `egress_*`, `ingress_*`)
  come from the docs, not the proto.
- The worker model: `cli.run_app(WorkerOptions(entrypoint_fnc=...))` registers a
  worker with LiveKit Cloud; an empty `agent_name` auto-dispatches it to every room
  in the project. The hosted turn detector (`turn-detector-v1`) runs on LiveKit's
  inference when the worker runs against Cloud — no local model or extra key.
- SDK version drift is real and load-bearing: `livekit-plugins-anthropic` 1.7.1
  still constructs an `httpx` v1 client (rejected by `anthropic` 1.x on `httpx2`)
  and its prefill-suppression list stops at `claude-*-4-6`. Both are worked around
  in `agent/providers.py`.

### Not verified — still assumed, needs the webhook path or real accounts

- Which `disconnect_reason` codes LiveKit actually sends on the `participant_left`
  webhook (so which count as "ungraceful"). The receiver was tested only with a
  self-signed token; no live webhook has been delivered. Every real session
  currently gets `graceful_end = NULL`.
- Whether `metrics_collected` will be removed in a future `livekit-agents` minor
  (its docstring says it is deprecated for *usage* accounting → `session_usage_updated`;
  per-turn latency is also on `ChatMessage.metrics`). This project uses
  `metrics_collected` because it still fires and is the cleanest per-turn source.

### Reasoning by analogy from other platforms (BI / observability / SaaS health)

None of this is LiveKit-specific; it is carried over from dashboards I've built
before:

- week-over-week trend + a prior-window baseline is the right shape for a health
  signal;
- a red/amber/green *worst-of* rollup is what reads cleanly to an exec;
- usage-as-%-of-plan-limit is the cleanest expansion signal, and expansion is a
  *good* problem — it should never by itself make an account "At-risk";
- ratio-based signals (error rate ×baseline, p95 ×last week) need an absolute floor
  or they fire on noise — hence `min_abs` / `min_abs_ms`;
- the LLM should write the brief's prose but never the verdict.

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
  All 22 real LiveKit sessions currently land here, so none of them carry a
  graceful/ungraceful verdict yet — that needs the webhook path.
- **Peak concurrency** = max overlap of session `[start, end]` spans in the window
  (sweep line, `pipeline/rollups.py::_peak_concurrency`). A reconstruction from
  session bounds, not a figure LiveKit reports.

## Live vs simulated rows (`source` column)

Every telemetry row (`events`, `turn_metrics`, `quality_events`, `sessions`) carries
`source`: `live` (a real LiveKit Cloud call — agent worker joined a real room, real
`metrics_collected` / `connection_quality_changed` captured) or `sim`
(`scripts/simulate_accounts.py`). The simulator exists only because there is one
real LiveKit test project, not a portfolio; the scoring framework is identical for
both. The dashboard surfaces the split so a reviewer can see exactly which numbers
are real. Real calls are deliberately mixed into seeded accounts (`acme-corp`,
`globex`, `northwind`) — with real telemetry folded in, `acme-corp`'s verdict moves
(Healthy → At-risk) because its live calls run slower than its simulated history.

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
