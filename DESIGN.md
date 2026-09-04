# DESIGN — Portfolio Signal

Full content is written in Phase 6. This stub records the decisions already made so
they don't get lost between phases.

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

- **`total_latency_ms`** per turn = `eou + transcription_delay + llm.ttft + tts.ttfb`.
  A stand-in for "user stopped talking -> first agent audio", not a figure LiveKit
  reports directly.
- **Connection-quality time-at-level** = step function between change events (we only
  receive transitions, not a continuous sample).
- **Graceful vs ungraceful end** = inference from `disconnect_reason` + a
  disconnect-within-10s-of-join heuristic, pending confirmation of which reason codes
  LiveKit actually sends on the webhook.

## Scoring rules (as specified — implemented in Phase 3)

**Session verdict** (per room) = worst of latency / quality / stability sub-verdicts.
**Account verdict** (trailing 7d vs prior 7d) = `Healthy / Watch / At-risk` + one
top-reason string, from: % sessions red/amber + trend arrow; p95 TTFT WoW (regression
flag at +30%); error rate vs baseline; usage as % of plan limit (flag >75%,
"expansion signal" if >90% and rising).
