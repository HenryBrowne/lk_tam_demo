# Portfolio Signal

A Technical-Account-Manager instrument panel built on LiveKit session telemetry.
It ingests LiveKit voice-agent metrics from real calls (plus a webhook receiver for
room/participant events), rolls them up **per account**, scores account health with
deterministic rules, and surfaces a portfolio overview, a per-account drill-down,
and an auto-drafted exec "account brief". The point isn't the dashboard — it's that
every number on it maps to a specific thing a TAM would do next.

> **Framing:** I built this over a few days with no prior LiveKit exposure, as a
> portfolio piece for a TAM application. It's meant to show I ramp on an unfamiliar
> real-time stack quickly and that I think about the product the way someone who
> owns a book of voice-AI accounts would. It is **not** the work of a real-time
> systems expert, and nothing here should be read as deep WebRTC expertise. Where I
> relied on assumptions rather than verified behaviour, `NOTES-livekit-api.md` and
> `DESIGN.md` say so explicitly. Thresholds in `pipeline/thresholds.yaml` are
> **illustrative** — the scoring *framework* is the point, not the specific numbers.

## Status

Built in phases; this is a checkpointed build.

- [x] **Phase 1 — Scaffold + LiveKit API verification.** Repo, pinned venv,
      `.env.example`, `NOTES-livekit-api.md` (verified-vs-assumed API surface).
- [x] **Phase 2 — Ingest.** Webhook receiver (`receiver/app.py`, JWT + body-hash
      verify -> `events`) and voice agent + metrics hook (`agent/`, `metrics_collected`
      / `connection_quality_changed` -> `turn_metrics`, `quality_events`). SQLite
      schema in `pipeline/db.py`. **Live checkpoint passed:** a synthetic caller
      (`scripts/sim_call.py`) drove a real Deepgram->Claude->Cartesia call on LiveKit
      Cloud and 4 `turn_metrics` + 2 `quality_events` rows landed with real latency /
      token numbers. Webhook path verified offline (self-signed token); live webhook
      delivery still needs a tunnel + dashboard config. See `NOTES-livekit-api.md`.
- [x] **Phase 3 — Pipeline.** `pipeline/sessions.py` derives the `sessions` table
      from `events` (with a fallback for real calls that have no events yet);
      `pipeline/scoring.py` holds the deterministic session + account verdicts;
      thresholds live in `pipeline/thresholds.yaml` behind an ILLUSTRATIVE banner;
      `pipeline/rollups.py` rolls sessions up per account (trailing 7d vs prior 7d)
      and `python -m pipeline.rollups` prints the portfolio table.
      `scripts/simulate_accounts.py` seeds 5 accounts (one At-risk, one near its plan
      ceiling) with backdated telemetry; `scripts/load_test.sh` wraps `lk load-test`.
- [x] **Phase 4 — Dashboard.** `dashboard/streamlit_app.py` — a portfolio overview
      (KPI tiles, the account table with verdict / trend / top reason / usage bars,
      the signal→action key) and a per-account drill-down (signal cards paired with
      the TAM action, session-timeline scatter, latency histogram this-week-vs-prior,
      connection-quality seconds by level, turn-detail table, token cost). Reuses
      `pipeline.scoring` / `pipeline.rollups` so the numbers match the CLI.
- [x] **Phase 5 — Account brief.** `brief/llm_brief.py` + `brief/prompt.md` — the
      account's scored numbers → a tight `claude-sonnet-5` call → a 6–8 sentence
      exec summary. The deterministic rules make the verdict; the model only writes
      the prose (and the prompt forbids inventing metrics). Surfaced behind a
      "Generate brief" button in the dashboard drill-down; also
      `python -m brief.llm_brief --account <id>` (`--facts-only` for no API call).
- [x] **Phase 6 — Write-up.** The three sections below, plus the verified-vs-inferred
      split and scoring rationale in `DESIGN.md`.

## Signal -> TAM action

Every metric maps to something a TAM would actually do:

| Signal | Meaning | TAM action |
|---|---|---|
| Turn latency / time-to-first-token trending up | Config drifting off what scales | Proactive architecture review |
| Connection-quality "poor/lost" ratio rising | Customer edge / region / codec config | Design consultation |
| Ungraceful-disconnect rate spike | Something broke | Lead the escalation |
| Session-minutes / peak concurrency vs plan limit, trending up | Growth | Hand expansion signal to sales |
| Error rate vs 7-day baseline | Regression | Route to support/eng, keep customer leadership informed |

## Stack

Python 3.11+ (built on 3.12), single venv. FastAPI (webhook receiver only),
`livekit-agents` + `livekit-api`, a Deepgram -> Claude -> Cartesia voice agent
(providers swappable via env; all-OpenAI is a documented one-key fallback),
SQLite (stdlib `sqlite3`), Streamlit + Plotly, the Anthropic SDK for the brief.
No Docker, no queue, no cloud infra beyond LiveKit Cloud's free tier. Local-first.

## Setup

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env            # then fill in keys
```

You need: a LiveKit Cloud project (URL / API key / secret), an Anthropic API key,
and Deepgram + Cartesia keys for the voice agent. See `.env.example`.

## Running the ingest layer (Phase 2)

All commands run from the repo root with the venv active.

```bash
python -m pipeline.db          # create data/portfolio.db and print a summary

# 1. webhook receiver
python -m receiver.app         # listens on RECEIVER_PORT (default 8080)
```

Point your LiveKit project's webhook at the receiver. It needs a public URL, so for
local dev tunnel it (`cloudflared tunnel --url http://localhost:8080`, `ngrok http
8080`, or `lk`'s own forwarding), then in the LiveKit Cloud dashboard set
**Project → Settings → Webhooks** to `https://<tunnel>/livekit/webhook`.

```bash
# 2. voice agent (separate terminal)
python -m agent.main dev       # registers with LIVEKIT_URL, waits for a room

# 3. drive a call — either a synthetic caller (no mic needed):
python scripts/sim_call.py --account acme-corp
#   ...or talk to it yourself: open https://agents-playground.livekit.io,
#   connect it to your project, join a room and speak.

# 4. see telemetry land
python -m pipeline.db          # row counts + newest events / turn_metrics / quality_events
```

## Running the pipeline (Phase 3)

```bash
python scripts/simulate_accounts.py --seed 42   # seed 5 accounts + ~14d of backdated telemetry
python -m pipeline.sessions                      # (re)derive the sessions table
python -m pipeline.rollups                       # print the portfolio table

# optional: portfolio-scale load on LiveKit (needs `lk` + the receiver reachable)
scripts/load_test.sh globex
```

## Running the dashboard (Phase 4)

```bash
streamlit run dashboard/streamlit_app.py
```

Portfolio overview by default; pick an account in the sidebar for the drill-down.
"↻ Refresh data" re-reads the DB (run `simulate_accounts.py` / talk to the agent,
then refresh). Reads `data/portfolio.db` — seed it first with `simulate_accounts.py`.

## Live vs simulated data

Every `events` / `turn_metrics` / `quality_events` / `sessions` row carries a
`source`: **`live`** = a real LiveKit Cloud call (the agent worker joined a real
room and the SDK's `metrics_collected` / `connection_quality_changed` events were
captured), **`sim`** = generated by `scripts/simulate_accounts.py` for portfolio
breadth. The dashboard shows the split (a "N live" badge on the portfolio view, ◆
markers on the drill-down timeline, a `source` column in the turn table).

To add real rows: run the agent (`python -m agent.main dev`) and drive calls with
`scripts/sim_call.py` — one per account tag, `--degrade` for a rough one:

```bash
python scripts/sim_call.py --account acme-corp
python scripts/sim_call.py --account globex --degrade   # frame drop + jitter + barge-in + abrupt end
```

`simulate_accounts.py --reset` never deletes `live` rows (they live under an `RM_`
room_sid; synthetic rooms are `SIM_`).

`pipeline.rollups` runs `derive_sessions()` itself, so after a re-simulate you can just
re-run it. For the exec brief of one account:

```bash
python -m brief.llm_brief --account acme-corp             # one claude-sonnet-5 call
python -m brief.llm_brief --account acme-corp --facts-only # the DATA block only, no API call
```

Sample output:

```
ACCOUNT             VERDICT  TREND  SESS  RED/AMBER  p95 TTFT (WoW)  ERR vs base  USAGE min/conc  TOP REASON
------------------  -------  -----  ----  ---------  --------------  -----------  --------------  --------------------------------------------------
Acme Corp           At-risk  ↑      36    19%        0.6s (+55%)     0% (0.0x)    1% / 2%         p95 TTFT +55% WoW (0.4s->0.6s)
Initech LLC         At-risk  →      40    100%       1.2s (+4%)      10% (2.9x)   3% / 10%        100% of sessions red/amber (vs 100%)
Globex Corporation  Watch    ↑      55    27%        0.5s (+30%)     1% (0.8x)    3% / 7%         p95 TTFT +30% WoW (0.4s->0.5s)
Hooli Inc           Watch    →      128   9%         0.4s (-1%)      1% (0.9x)    95% / 50%       usage 95% of plan and rising - expansion signal
Northwind Trading   Healthy  ↑      61    5%         0.4s (+2%)      1% (2.8x)    2% / 4%         all signals within thresholds
```

(Acme Corp is At-risk here because its 3 real LiveKit calls this week ran slower
than its seeded history — real telemetry moving a verdict.)

## What I learned about LiveKit building this

Honest about what I verified against the SDK / a real call vs. what I inferred.
`DESIGN.md` and `NOTES-livekit-api.md` have the line-by-line version.

- **Webhook model.** LiveKit signs each webhook as a JWT in the `Authorization`
  header whose claims carry a `sha256` of the body; you verify with
  `livekit.api.WebhookReceiver` over the **raw** body bytes (a re-serialised parse
  fails the hash). Events are `room_started/finished`,
  `participant_joined/left/connection_aborted`, `track_*`, `egress_*`, `ingress_*`.
  I verified the helper and the event list; I have **not** run live webhook
  delivery (that needs a public tunnel + a URL set in the Cloud dashboard), so
  which `disconnect_reason` values mean "ungraceful" is still an assumption and
  every real session currently gets `graceful_end = NULL`.
- **Agent metrics surface.** `AgentSession` emits `metrics_collected` with
  per-component objects — `LLMMetrics.ttft`, `TTSMetrics.ttfb`,
  `EOUMetrics.end_of_utterance_delay` / `transcription_delay`, all in **seconds**.
  In `livekit-agents` 1.7.1 that event still fires but its docstring calls it
  deprecated for *usage* accounting (→ `session_usage_updated`), and per-turn
  latency also lives on `ChatMessage.metrics` now — I used `metrics_collected`
  because it is still the cleanest per-turn source. EOU/LLM/TTS for one turn share
  a `speech_id`; that is the only thing tying a "turn" together. Confirmed against
  real calls: haiku-4-5 TTFT ~0.65 s, sonnet-4-6 ~1.1 s.
- **Connection-quality semantics.** `room.on("connection_quality_changed")` gives
  `(participant, quality)` with `EXCELLENT/GOOD/POOR/LOST/UNKNOWN`. You get
  *transitions only* — no continuous sample — so "share of the call at poor/lost"
  is a step function between events. The agent does receive these for the remote
  (human) participant, not just itself. From localhost I could never actually
  provoke `poor`/`lost` on the caller link.
- **SFU basics (inferred, not expertise).** A room is a selective-forwarding unit:
  the agent joins as another participant, media goes client → SFU → agent, and the
  connection-quality reading is the SFU's estimate of each peer's link, not an
  end-to-end path measurement. The worker model: `cli.run_app(WorkerOptions(...))`
  registers a worker with Cloud; an empty `agent_name` auto-dispatches it to every
  room. The hosted turn detector runs on LiveKit inference when you're on Cloud.
- **Version drift is real** — exactly what the "SDKs change across versions" hint
  was pointing at. `livekit-plugins-anthropic` 1.7.1 still wires an `httpx` v1
  client (rejected by `anthropic` 1.x, which is on `httpx2`) and its
  prefill-suppression list stops at `claude-*-4-6`. Both are handled in
  `agent/providers.py`; both are the kind of thing a TAM would need to spot fast
  when a customer's build breaks after an SDK bump.
- **What's simulated.** I have one LiveKit test project, not a book of accounts, so
  portfolio breadth (`scripts/simulate_accounts.py`) is fabricated and labelled
  `source = sim`. Six sessions across three accounts are real LiveKit Cloud calls
  (`source = live`), marked as such throughout the UI.

## What I'd build next with access to real accounts

- **Close the webhook loop** — tunnel + the Cloud dashboard webhook — so `events`
  is real: accurate session start/end, participant counts, and a real
  graceful/ungraceful verdict instead of `NULL`.
- **LiveKit Cloud Analytics API** instead of reconstructing session history from
  webhooks — authoritative session-minutes, peak concurrency, and per-region data.
- **Per-region breakdown.** Latency and connection quality are regional; every
  signal should split by the participant's `region`, and "poor quality in
  eu-central only" is a very different TAM conversation than "poor everywhere".
- **Thresholds co-owned with the customer.** One `thresholds.yaml` per account,
  set to *their* product SLO and contract, agreed in the QBR and revisited each
  quarter — the dashboard would show "your bar", not my illustrative one.
- **Wire the output into the workflow.** A verdict flip → a Slack/email to the
  owning TAM; the account brief → a slide exported straight into the QBR deck;
  the "expansion signal" → a task on the AE.
- **Drop the simulator.** Keep the deterministic scoring, replace fabricated
  breadth with real fleet data, and run `lk load-test` at real portfolio scale for
  capacity planning.

## Time spent

About 3 days, from no prior LiveKit exposure — most of it on the API-verification
pass (`NOTES-livekit-api.md`) and the two SDK-drift workarounds, not the analytics,
which is familiar ground.
