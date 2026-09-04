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

`pipeline.rollups` runs `derive_sessions()` itself, so after a re-simulate you can just
re-run it. Sample output:

```
ACCOUNT             VERDICT  TREND  SESS  RED/AMBER  p95 TTFT (WoW)  ERR vs base  USAGE min/conc  TOP REASON
------------------  -------  -----  ----  ---------  --------------  -----------  --------------  ---------------------------------------------
Initech LLC         At-risk  →      40    100%       1.2s (+4%)      10% (2.9x)   3% / 10%        100% of sessions red/amber (vs 100%)
Globex Corporation  Watch    ↑      53    25%        0.5s (+25%)     1% (0.8x)    3% / 7%         p95 TTFT +25% WoW (0.4s->0.5s)
Hooli Inc           Watch    →      130   9%         0.4s (-1%)      1% (0.8x)    97% / 50%       usage 97% of plan and rising - expansion signal
Acme Corp           Healthy  →      34    15%        0.4s (+12%)     0% (0.0x)    1% / 2%         all signals within thresholds
Northwind Trading   Healthy  ↑      63    3%         0.4s (-1%)      1% (2.5x)    2% / 4%         all signals within thresholds
```

_Full "what I learned about LiveKit", "what I'd build next with real accounts", and
time-spent notes land here in Phase 6._
