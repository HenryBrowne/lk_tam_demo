# Portfolio Signal

A Technical-Account-Manager instrument panel built on LiveKit session telemetry.
It ingests LiveKit webhooks + voice-agent metrics, rolls them up **per account**,
scores account health with deterministic rules, and surfaces a portfolio overview,
a per-account drill-down, and an auto-drafted exec "account brief".

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
- [~] **Phase 2 — Ingest.** Webhook receiver (`receiver/app.py`, JWT + body-hash
      verify -> `events`) and voice agent + metrics hook (`agent/`, `metrics_collected`
      / `connection_quality_changed` -> `turn_metrics`, `quality_events`). SQLite
      schema in `pipeline/db.py`. Verify flow and metric-assembly are unit-tested;
      **live "talk to the agent" checkpoint pending keys.**
- [ ] Phase 3 — Pipeline (derive sessions, scoring, rollups, simulator, load test)
- [ ] Phase 4 — Streamlit dashboard
- [ ] Phase 5 — LLM account brief
- [ ] Phase 6 — Write-up (this file + `DESIGN.md` get their full content here)

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
#   first run may download a ~108MB local turn-detector model

# 3. talk to it
#   open https://agents-playground.livekit.io, connect it to your project,
#   join a room and speak — or:  lk room join --publish-mic --identity me <room>

# 4. see telemetry land
python -m pipeline.db          # row counts + newest events / turn_metrics / quality_events
```

_Full "what I learned about LiveKit", "what I'd build next with real accounts", and
time-spent notes land here in Phase 6._
