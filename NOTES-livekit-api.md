# NOTES — LiveKit API verification

**Purpose:** before writing any ingest code, pin down the *real* shape of the LiveKit
APIs this project depends on, at the versions actually installed. LiveKit's SDKs moved
a lot of surface between 0.x and 1.x; this file records what was checked against the
installed source and the live docs, versus what is still an assumption to confirm at
the Phase 2 checkpoint.

I am not a real-time-systems expert — I picked this stack up over a few days for this
portfolio piece. Where I wasn't sure, it says so, with a `TODO(livekit-api)` and a link.

**Verification date:** 2026-09-04
**Method:** read the installed package source under `.venv/Lib/site-packages/livekit/`
(authoritative for the pinned versions) + cross-checked event names / auth model
against `docs.livekit.io`.

---

## Installed versions (pinned in `requirements.txt`, full freeze in `requirements.lock.txt`)

| Package | Version | Notes |
|---|---|---|
| `livekit-agents` | 1.7.1 | latest on PyPI at build time |
| `livekit-api` | 1.2.1 | |
| `livekit` (rtc SDK, transitive) | 1.1.15 | pulled by `livekit-agents` |
| `livekit-protocol` | 1.1.26 | protobuf models |
| `livekit-plugins-deepgram` | 1.7.1 | STT (default) |
| `livekit-plugins-cartesia` | 1.7.1 | TTS (default) |
| `livekit-plugins-anthropic` | 1.7.1 | LLM node (default) — see caveat below |
| `livekit-plugins-openai` | 1.7.1 | kept only for the all-OpenAI fallback |
| `livekit-plugins-silero` | 1.7.1 | VAD |
| `livekit-plugins-turn-detector` | 1.7.1 | end-of-utterance model |
| `anthropic` (SDK) | 1.3.0 | used directly by the brief generator |

Python 3.12.10, Windows. `py -3.12 -m venv .venv`.

---

## 1. Webhook receiver — `livekit-api`

### VERIFIED (source: `livekit/api/webhook.py`, `livekit/api/access_token.py`)

- Helper class is **`livekit.api.WebhookReceiver`**, constructed with a
  **`livekit.api.TokenVerifier`**:

  ```python
  from livekit.api import TokenVerifier, WebhookReceiver

  verifier = TokenVerifier(api_key, api_secret)      # both args optional;
                                                     # falls back to env
                                                     # LIVEKIT_API_KEY / LIVEKIT_API_SECRET
  receiver = WebhookReceiver(verifier)

  event = receiver.receive(body: str, auth_token: str)   # -> livekit.protocol.webhook.WebhookEvent
  ```

- `receiver.receive()` does three things (read from source):
  1. `verifier.verify(auth_token)` — validates the JWT signature + claims.
  2. checks `claims.sha256` is present, base64-decodes it, and compares it to
     `sha256(body)` — **raises `Exception("hash mismatch")` if the body was tampered with**.
  3. `google.protobuf.json_format.Parse(body, WebhookEvent(), ignore_unknown_fields=True)`.
- The `auth_token` is the value of the incoming **`Authorization`** header (raw JWT,
  no `Bearer ` prefix — LiveKit sends it bare; strip a prefix defensively).
- `body` must be the **raw request body string**, unmodified — the hash check is over
  exact bytes. In FastAPI: `await request.body()` then `.decode()`, do **not** use the
  parsed JSON.

### VERIFIED (source: `WebhookEvent.DESCRIPTOR`, live docs)

`WebhookEvent` protobuf fields:
`event`, `room`, `participant`, `egress_info`, `ingress_info`, `track`, `job`, `id`,
`created_at`, `num_dropped`.

Event name strings (from https://docs.livekit.io/home/server/webhooks/ — **not**
enumerable from the proto, which types `event` as a bare string):

| `event` value | payload fields populated |
|---|---|
| `room_started` | `room` |
| `room_finished` | `room` |
| `participant_joined` | `room`, `participant` |
| `participant_left` | `room`, `participant` |
| `participant_connection_aborted` | `room`, `participant` |
| `track_published` | `room`, `participant`, `track` |
| `track_unpublished` | `room`, `participant`, `track` |
| `egress_started` / `egress_updated` / `egress_ended` | `egress_info` |
| `ingress_started` / `ingress_ended` | `ingress_info` |

All events also carry `id` (UUID), `created_at` (unix seconds), `event`.

- `Room` proto fields (verified): `sid`, `name`, `metadata`, `num_participants`,
  `num_publishers`, `creation_time`, `max_participants`, `active_recording`, ...
- `ParticipantInfo` proto fields (verified): `sid`, `identity`, `state`, `metadata`,
  `joined_at`, `name`, `disconnect_reason`, `attributes`, `kind`, `region`, ...

### RELEVANCE TO PORTFOLIO SIGNAL

- `room_started` / `room_finished` → session start/end + `duration_s`.
- `participant_left` with a `disconnect_reason` other than a clean client leave, and
  `participant_connection_aborted`, → **ungraceful disconnect** signal.
- `room.metadata` on `room_started` is where we read `{"account_id": "..."}`.

### TODO(livekit-api)

- [ ] Confirm at runtime whether LiveKit Cloud populates `participant.disconnect_reason`
      on the `participant_left` webhook payload, or only on the in-room rtc event.
      Doc: https://docs.livekit.io/home/server/webhooks/  — payload detail is thin.
- [ ] Confirm the exact `DisconnectReason` values that indicate "ungraceful" vs a normal
      hangup. Enum values seen in `livekit.rtc.DisconnectReason` (rtc SDK, may differ
      from the webhook's `models.proto` copy): `CLIENT_INITIATED`, `DUPLICATE_IDENTITY`,
      `SERVER_SHUTDOWN`, `PARTICIPANT_REMOVED`, `ROOM_DELETED`, `STATE_MISMATCH`,
      `JOIN_FAILURE`, `MIGRATION`, `SIGNAL_CLOSE`, `ROOM_CLOSED`, `USER_UNAVAILABLE`,
      `USER_REJECTED`, `SIP_TRUNK_FAILURE`, `CONNECTION_TIMEOUT`, `MEDIA_FAILURE`,
      `AGENT_ERROR`. Working assumption: `CLIENT_INITIATED` + unset = graceful;
      everything else = ungraceful. To be validated with a load-test run.

---

## 2. Agent metrics — `livekit-agents`

### VERIFIED (source: `livekit/agents/metrics/base.py`, `livekit/agents/voice/events.py`)

Metrics are emitted on the **`AgentSession`** as the **`metrics_collected`** event
(still present in `EventTypes` in 1.7.1):

```python
from livekit.agents import metrics
from livekit.agents.metrics import LLMMetrics, STTMetrics, TTSMetrics, EOUMetrics

@session.on("metrics_collected")
def _on_metrics(ev):              # ev: MetricsCollectedEvent
    m = ev.metrics                # one of the AgentMetrics union members
    metrics.log_metrics(m)        # built-in pretty logger, optional
    # ... our code: branch on type and write a row
```

`MetricsCollectedEvent` = `{ type: "metrics_collected", metrics: AgentMetrics,
created_at: float }`.

`AgentMetrics` union members and the fields Portfolio Signal uses (all times in
**seconds**, converted to ms on write):

| Class | `type` literal | key fields |
|---|---|---|
| `LLMMetrics` | `llm_metrics` | `ttft` (time to first token, s; `-1` if none), `duration`, `prompt_tokens`, `completion_tokens`, `prompt_cached_tokens`, `total_tokens`, `tokens_per_second`, `request_id`, `speech_id`, `cancelled` |
| `STTMetrics` | `stt_metrics` | `duration` (0.0 if streaming), `audio_duration`, `streamed`, `request_id` |
| `TTSMetrics` | `tts_metrics` | `ttfb` (time to first byte, s), `duration`, `audio_duration`, `characters_count`, `cancelled`, `segment_id`, `speech_id` |
| `EOUMetrics` | `eou_metrics` | `end_of_utterance_delay` (s; 0.0 if end-of-speech not detected), `transcription_delay` (s), `on_user_turn_completed_delay` (s), `speech_id` |
| `VADMetrics` | `vad_metrics` | `idle_time`, `inference_duration_total`, `inference_count` — not used |
| `EOTInferenceMetrics` | `eot_inference_metrics` | per-inference turn-detector timing — not used for now |
| `RealtimeModelMetrics` | `realtime_model_metrics` | only for the Realtime API path — N/A, we use the STT->LLM->TTS pipeline |
| `InterruptionMetrics`, `AvatarMetrics` | | not used |

Each metric also has an optional `.metadata` with `model_name` / `model_provider`.

### Mapping to our `turn_metrics` table

A "turn" = one user utterance → one agent reply. The pipeline emits `EOUMetrics`,
`LLMMetrics`, `TTSMetrics` (and `STTMetrics`) events that share a `speech_id`
(EOU/LLM/TTS) — we **correlate by `speech_id`** within a room to assemble a turn row.

| column | source |
|---|---|
| `ttft_ms` | `LLMMetrics.ttft * 1000` |
| `ttfb_ms` | `TTSMetrics.ttfb * 1000` |
| `eou_ms` | `EOUMetrics.end_of_utterance_delay * 1000` |
| `total_latency_ms` | **derived** = `eou_ms + transcription_delay*1000 + ttft_ms + ttfb_ms` — an approximation of user-perceived "stopped talking → first agent audio". Documented as such in DESIGN.md. |
| `llm_tokens_in` | `LLMMetrics.prompt_tokens` |
| `llm_tokens_out` | `LLMMetrics.completion_tokens` |
| `error_flag` | `LLMMetrics.cancelled` OR `TTSMetrics.cancelled` OR an exception captured on the `error` session event |

### PARTIALLY VERIFIED — deprecation nuance

`MetricsCollectedEvent`'s docstring in 1.7.1 reads:

> "Deprecated: use `session_usage_updated` for usage tracking. Per-turn latency
> metrics are available on `ChatMessage.metrics`."

Reading of this (to confirm): the **`metrics_collected` event still fires and still
carries the per-component STT/LLM/TTS/EOU objects** — it is only *deprecated as the
recommended path for aggregate usage/token accounting*, which now has a dedicated
`session_usage_updated` → `SessionUsageUpdatedEvent.usage: AgentSessionUsage`
(per-model token/character/audio totals — verified in `metrics/usage.py`). There is
also a newer `ChatMessage.metrics: MetricsReport` on each `conversation_item_added`
item (verified the field exists in `llm/chat_context.py`; its `MetricsReport` shape
not yet inspected in detail).

**Decision for this project:** use `metrics_collected` as the primary capture (it's
the cleanest per-turn latency source and still supported in 1.7.1), and additionally
listen to `session_usage_updated` for authoritative token/char totals. If a future
`livekit-agents` minor removes `metrics_collected`, the migration is to
`ChatMessage.metrics`.

### TODO(livekit-api)

- [ ] Inspect `MetricsReport` (the type of `ChatMessage.metrics`) and note whether it
      is a strict superset of what `metrics_collected` gives per turn — if so, switch
      to it now rather than later. Source: `livekit/agents/llm/chat_context.py`,
      and https://docs.livekit.io/agents/build/metrics/
- [ ] Confirm Deepgram (`STTMetrics`) and Cartesia (`TTSMetrics`) actually populate
      `ttfb` / `duration` and not just zeros — verify against real rows at the Phase 2
      checkpoint.
- [ ] Confirm which event carries end-to-end turn latency directly, if any (vs. our
      derived sum). Docs: https://docs.livekit.io/agents/build/metrics/

---

## 3. Connection quality — `livekit` (rtc SDK, used inside the agent)

### VERIFIED (source: `livekit/rtc/room.py`)

- Event name: **`connection_quality_changed`** on the `rtc.Room` object.
- Handler signature: `(participant: rtc.Participant, quality: rtc.ConnectionQuality)`.

  ```python
  @ctx.room.on("connection_quality_changed")
  def _on_quality(participant, quality):
      # quality is an int enum; map to a label and write a quality_events row
      ...
  ```

- `rtc.ConnectionQuality` values (verified): `QUALITY_EXCELLENT`, `QUALITY_GOOD`,
  `QUALITY_POOR`, `QUALITY_LOST`, `QUALITY_UNKNOWN`.
- Related room events that exist (verified in `EventTypes`): `participant_connected`,
  `participant_disconnected`, `disconnected` (carries a `DisconnectReason`),
  `reconnecting`, `reconnected`, `room_metadata_changed`.

### Mapping to `quality_events`

Write one row per `connection_quality_changed`: `(ts, room_sid, account_id,
participant_id, quality)` where `quality` ∈ {`excellent`,`good`,`poor`,`lost`,`unknown`}
(strip the `QUALITY_` prefix, lowercase).

Scoring needs "share of session-seconds at poor|lost". We only get *change* events, so
we treat each event as the start of an interval that runs until the next event (or
session end) — a step function. Documented as an approximation in DESIGN.md.

### TODO(livekit-api)

- [ ] Confirm the agent (a server participant) actually *receives*
      `connection_quality_changed` for the **remote** (human) participant, not just
      for itself. If Cloud only reports the agent's own quality, we get less signal
      than hoped — note this honestly. Docs:
      https://docs.livekit.io/home/client/events/  and
      https://docs.livekit.io/reference/python/v1/livekit/rtc/index.html
- [ ] `lk load-test` generates connection-quality data at scale — confirm it surfaces
      through the same event or only via server stats. Docs:
      https://docs.livekit.io/home/cli/load-test/

---

## 4. Room -> account tagging

### VERIFIED

- Room metadata is a string field on `Room` (`Room.metadata`), settable at room
  creation and readable from the `room_started` webhook and from `ctx.room.metadata`
  inside the agent (`JobContext.room` -> `rtc.Room`, verified in `livekit/agents/job.py`).
- Convention for this project: room metadata JSON `{"account_id": "globex"}`.
- Fallback: parse the participant identity, splitting on `ACCOUNT_IDENTITY_PREFIX_SEP`
  (default `__`), e.g. `acct-globex__user-42` -> `globex`. Pure project convention,
  nothing LiveKit-specific.

### TODO(livekit-api)

- [ ] `simulate_accounts.py` and the load-test wrapper must set room metadata when
      they create rooms — confirm `livekit.api.RoomService.create_room(...)` accepts a
      `metadata=` arg at v1.2.1. Source to check: `livekit/api/room_service.py`.

---

## 5. Agent entrypoint / worker (for Phase 2)

### VERIFIED (top-level `livekit.agents` exports present in 1.7.1)

`cli`, `WorkerOptions`, `JobContext`, `JobProcess`, `Agent`, `AgentSession`,
`RoomInputOptions`, `RoomOutputOptions`, `metrics`, `WorkerType`.

Shape (standard 1.x pattern, to be finalised against a working run in Phase 2):

```python
from livekit.agents import cli, WorkerOptions, JobContext, AgentSession, Agent

async def entrypoint(ctx: JobContext):
    await ctx.connect()
    session = AgentSession(stt=..., llm=..., tts=..., vad=...)
    # wire @session.on("metrics_collected") / @ctx.room.on("connection_quality_changed")
    await session.start(agent=Agent(instructions="..."), room=ctx.room)

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
```

### TODO(livekit-api)

- [ ] Confirm `AgentSession.start(...)` signature and whether `room=` / `agent=` are the
      current kwargs at 1.7.1 (constructor has a big `@deprecate_params` block —
      several `min_endpointing_delay` etc. args moved to `turn_handling=`). Source:
      `livekit/agents/voice/agent_session.py`. Get this from a real run, not guesswork.
- [ ] Confirm `cli.run_app` vs `cli.run_app(WorkerOptions(...))` and how the worker
      authenticates to LiveKit Cloud (env `LIVEKIT_URL/API_KEY/API_SECRET`).
      Docs: https://docs.livekit.io/agents/

---

## 6. LLM provider caveat — `livekit-plugins-anthropic` 1.7.1 + Claude 5-family models

### VERIFIED (source: `livekit/plugins/anthropic/llm.py`, `llm/models.py`)

- The plugin's `ChatModels` type hint (`models.py`) is **stale** — its newest entries
  are `claude-sonnet-4-6` / `claude-opus-4-6`. It has no Claude 5 / Haiku 4.5 entries.
- BUT the `LLM(model=...)` parameter is typed `str | ChatModels`, so passing a newer
  model string (e.g. `"claude-haiku-4-5"`) is accepted and forwarded as-is.
- The installed **`anthropic` SDK 1.3.0** *does* know the current model IDs — its
  `Model` literal includes `claude-sonnet-5`, `claude-opus-5`, `claude-haiku-4-5`,
  `claude-haiku-4-5-20251001`, `claude-fable-5-1`, etc. So the request will be sent.
- **Prefill hazard:** the plugin decides whether to inject a trailing user message
  based on `_NO_PREFILL_PATTERNS = ("claude-sonnet-4-6", "claude-opus-4-6")` matched
  with `str.startswith`. For any other model — including `claude-haiku-4-5`,
  `claude-sonnet-5`, `claude-opus-5` — it does **not** inject, so if a chat context
  ever ends on an assistant turn the plugin will send an assistant-message prefill.
  Per the Anthropic API, prefill returns **HTTP 400** on Sonnet 5 / Opus 5 / the 4.6+
  family. Haiku 4.5 still tolerates prefill.

### DECISION

- **Agent LLM default: `claude-haiku-4-5`** (`AGENT_LLM_MODEL`). Rationale: lowest
  latency for real-time, *and* it sidesteps the plugin's prefill bug because Haiku 4.5
  still accepts prefill. In the normal voice turn flow the LLM is always called right
  after a user turn (context ends on a user message), so a prefill shouldn't be
  injected anyway — Haiku 4.5 just makes it safe if that assumption ever breaks
  (preemptive generation, interrupted/continued turns).
- **Brief generator: `claude-sonnet-5`** (`BRIEF_LLM_MODEL`) via the **`anthropic` SDK
  directly** — we fully control that request (no prefill, no `budget_tokens`), so the
  newest Sonnet is fine and appropriate for an exec-facing summary.
- If `claude-haiku-4-5` misbehaves through the plugin at the Phase 2 checkpoint, the
  fallback is `AGENT_LLM_PROVIDER=openai` (kept installed) or pinning the agent to
  `claude-sonnet-4-6` (a model the plugin's prefill logic handles correctly).

### TODO(livekit-api)

- [ ] At the Phase 2 checkpoint, verify a real conversation turn completes through
      `livekit-plugins-anthropic` with `claude-haiku-4-5` and that `LLMMetrics` rows
      have sane `ttft` / token counts.
- [ ] Watch for the plugin stripping `<thinking>...</thinking>` from responses
      (it has explicit handling for that) — shouldn't matter for a voice agent, but
      note it if transcripts look truncated.

---

## Phase 2 verification update (2026-09-04)

Built the ingest layer. What got *proven* (not just read):

- **Webhook verify flow — proven end-to-end.** A test signs a body with
  `AccessToken(key, secret).with_sha256(b64(sha256(body))).to_jwt()` and posts it to
  the FastAPI receiver via `TestClient`. Results:
  - valid token + matching hash -> `WebhookReceiver.receive()` returns the parsed
    `WebhookEvent`, row written to `events`. ✅
  - garbage `Authorization` -> `receive()` raises -> receiver returns 401. ✅
  - valid signature but body mutated by one byte after signing -> `receive()` raises
    `"hash mismatch"` -> 401. ✅
  - `event.HasField("room")` / `event.HasField("participant")` are the right guards
    (protobuf) before reading those sub-messages.
  - account tagging: `{"account_id": "globex"}` in `room.metadata` resolves; with no
    metadata, `acct-initech__caller-9` -> `initech` via the identity fallback. ✅
- **Metric object shapes — proven by construction.** `EOUMetrics`, `LLMMetrics`,
  `STTMetrics`, `TTSMetrics` are pydantic models importable from
  `livekit.agents.metrics`; constructing them with the fields listed in section 2 and
  running them through `MetricsSink` produces correct `turn_metrics` rows (ms
  conversion, `ttft=-1` -> NULL, `total_latency_ms` = sum of available parts,
  `cancelled` -> `error_flag`). `STTMetrics` has no `speech_id` — confirmed, so it
  can't be tied to a turn; usage totals will come from `session_usage_updated`.
- **`MetricsCollectedEvent`** imports from `livekit.agents` (not `.metrics`); shape
  `{type, metrics, created_at}`. ✅
- **`AgentSession.start(agent, *, room=, record=, ...)`** — `room_input_options` /
  `room_output_options` are now deprecated in favour of `room_options`. Passing
  `record=False` to avoid egress on the free tier.
- **`rtc.Room.sid` is a coroutine** (`await ctx.room.sid`), not a property.
- **Default turn detector is safe to leave unset.** `livekit.agents.inference.TurnDetector`
  (the non-deprecated path; `livekit-plugins-turn-detector` is deprecated) auto-selects:
  LiveKit Cloud inference when `LIVEKIT_INFERENCE_URL` + key/secret are present (they
  are, on a hosted run), else it downloads a local ~108 MB `v1-mini` model, else it
  commits turns on the endpointing delay. It never hard-fails in `auto` mode.
  `EOUMetrics` fires in all three cases.
- **Deprecated:** `livekit.plugins.turn_detector` warns on import — noted; not used.
  `Agent(turn_detection=...)` / `AgentSession(turn_detection=...)` are deprecated in
  favour of `turn_handling=TurnHandlingOptions(...)`.

Still **not** verified (needs a live LiveKit Cloud run — the Phase 2 checkpoint):

- That a real `metrics_collected` stream from Deepgram + Claude + Cartesia populates
  `ttft` / `ttfb` / `end_of_utterance_delay` with real numbers (not zeros), and that
  EOU/LLM/TTS actually share one `speech_id` per turn (the sink assumes this).
- Whether the agent receives `connection_quality_changed` for the **remote** human
  participant (vs only itself).
- Whether LiveKit Cloud sends `disconnect_reason` on the `participant_left` webhook.
- Exact `cli.run_app` / worker registration behaviour against the real project.

---

## Phase 2 checkpoint — LIVE RUN results (2026-09-04, project `tam-monitor`)

Ran `python -m agent.main dev` + `scripts/sim_call.py` (a synthetic caller that
joins a room tagged `{"account_id": "acme-corp"}` and speaks 3 Cartesia-synthesized
turns). Agent = Deepgram nova-3 -> Claude `claude-haiku-4-5` -> Cartesia sonic-3.

**Result: 4 `turn_metrics` rows + 2 `quality_events` rows written, all with
`account_id='acme-corp'`.** Representative row:

```
ttft_ms=646.7  ttfb_ms=211.3  eou_ms=577.0  total_latency_ms=1689.6
llm_tokens_in=104  llm_tokens_out=46  error_flag=0
```

### Now CONFIRMED (was assumed)

- **EOU / LLM / TTS share one `speech_id` per turn.** The sink's correlation key
  works. (The agent's opening greeting produces an LLM+TTS pair with **no** EOU —
  `eou_ms` is NULL for that row, which is correct: no user turn preceded it.)
- **The agent receives `connection_quality_changed` for the remote (caller)
  participant**, not just itself — got a `quality_events` row for
  `acct-acme-corp__sim-caller`. So this signal is usable.
- **`turn-detector-v1` (LiveKit hosted inference) works with only
  `LIVEKIT_URL/API_KEY/API_SECRET`** — `EOUMetrics.metadata.model_name` came back as
  `turn-detector-v1`, `model_provider = livekit`. No extra inference key or local
  model download was needed on a hosted run. `end_of_utterance_delay ≈ 0.58s`
  consistently.
- **Latency-field units confirmed as seconds** by cross-checking the SDK's own
  "LLM metrics" / "TTS metrics" log lines (`ttft: 0.76`) against our stored
  `ttft_ms=763.0`.
- Deepgram `STTMetrics` fire but carry `audio_duration` only (no per-request latency
  when streaming, as documented) and **no `speech_id`** — so STT stays out of turn
  assembly, as designed. Token/usage totals will come from `session_usage_updated`.
- `InterruptionMetrics` (`model: "adaptive interruption"`, provider `livekit`) also
  stream — not used by Portfolio Signal, ignored by the sink.
- Graceful disconnect: session closed with
  `reason="participant_disconnected", error=null`.

### Two real bugs found and fixed (see `agent/providers.py`)

1. **`RuntimeError: Plugins must be registered on the main thread`.** Lazily
   importing `livekit.plugins.*` inside the factory functions (which run on a job
   thread) fails — `Plugin.register_plugin()` must run on the main thread. Fix:
   import all plugin packages at module top level in `providers.py`.
2. **`TypeError: Invalid http_client argument; Expected httpx2.AsyncClient but got
   httpx.AsyncClient`.** `livekit-plugins-anthropic` 1.7.1 (`Requires-Dist:
   anthropic>=0.41`, `httpx`) hard-codes an `httpx` (v1) client, but `anthropic`
   1.3.0 is built on `httpx2` and rejects it. Fix: pass our own
   `client=anthropic.AsyncAnthropic()` to `anthropic.LLM(...)` — the plugin uses
   `client or <its own>`, so this bypasses the broken wiring and keeps us on the
   current SDK (which Phase 5's brief also uses). Reproducible because
   `requirements.lock.txt` pins both.

### Still open

- **Webhook `events`** — not exercised live yet (needs a public tunnel + the
  LiveKit dashboard webhook pointed at `/livekit/webhook`). Verified offline with a
  self-signed token; unchanged.
- **`disconnect_reason` on the `participant_left` webhook** — still unverified;
  needs the webhook path running during a call.
- **`enable_recording: true`** appears in the job-dispatch payload (a project-level
  setting on `tam-monitor`). `session.start(record=False)` is set on our side; the
  room-level auto-egress is a separate dashboard toggle. No errors seen from it, but
  worth turning off in the dashboard if egress minutes matter.
- The sim caller's fixed inter-turn gaps make the agent log occasional
  `InterruptionMetrics` (it briefly thinks the caller barged in). Cosmetic; real
  human turns won't do this.

### Phase 3 note

- **`lk` (the LiveKit CLI) is not installed in this environment**, so
  `scripts/load_test.sh` is written and its preflight is exercised, but a real
  `lk load-test` run has not happened. The Phase 3 portfolio table is populated by
  `scripts/simulate_accounts.py` (synthetic, deterministic) plus the real
  `acme-corp` turns from the Phase 2 checkpoint (which reach `sessions` via the
  fallback-synthesis path in `pipeline/sessions.py`).

---

## Summary — what's solid vs what needs the Phase 2 run

**Solid (read straight from installed source + confirmed in docs):**
- `WebhookReceiver` / `TokenVerifier` API and the body-hash verification flow.
- Webhook event name list and which payload fields each carries.
- `metrics_collected` event + the `LLMMetrics` / `STTMetrics` / `TTSMetrics` /
  `EOUMetrics` field names and units (seconds).
- `connection_quality_changed` event name, handler signature, and the
  `QUALITY_{EXCELLENT,GOOD,POOR,LOST,UNKNOWN}` values.
- Room metadata is the right place to carry `account_id`.
- The `livekit-plugins-anthropic` prefill hazard and why Haiku 4.5 avoids it.

**Needs confirmation with a live LiveKit Cloud project + `lk load-test` (Phase 2):**
- Whether `disconnect_reason` reaches the webhook and which values mean "ungraceful".
- Whether the agent receives `connection_quality_changed` for the *remote* participant.
- Exact `AgentSession.start()` / `cli.run_app` signatures at 1.7.1.
- That Deepgram/Cartesia populate the latency fields (not zeros).
- Whether `ChatMessage.metrics` should replace `metrics_collected` now rather than later.
- `RoomService.create_room(metadata=...)` accepted at `livekit-api` 1.2.1.
