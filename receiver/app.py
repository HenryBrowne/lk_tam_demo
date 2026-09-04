"""FastAPI webhook receiver for LiveKit Cloud.

Its only job: verify each incoming webhook (JWT signature + body sha256, via
``livekit.api.WebhookReceiver``) and append one row to the ``events`` table.
Everything downstream reads from SQLite, not from here.

Run:  python -m receiver.app       (reads RECEIVER_HOST / RECEIVER_PORT from env)
  or  uvicorn receiver.app:app --port 8080
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request, Response
from livekit.api import TokenVerifier, WebhookReceiver

from pipeline.db import connect, iso
from pipeline.tagging import resolve_account_id

load_dotenv()

_verifier = TokenVerifier(
    os.environ.get("LIVEKIT_API_KEY"),
    os.environ.get("LIVEKIT_API_SECRET"),
)
_webhook = WebhookReceiver(_verifier)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the DB / schema once at startup so the first webhook doesn't race it.
    conn = connect()
    conn.close()
    yield


app = FastAPI(title="Portfolio Signal — webhook receiver", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/livekit/webhook")
async def livekit_webhook(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    # The hash check is over the exact bytes LiveKit sent — use the raw body,
    # never a re-serialised parse.
    body = (await request.body()).decode("utf-8")
    token = (authorization or "").removeprefix("Bearer ").strip()

    try:
        event = _webhook.receive(body, token)
    except Exception as exc:  # livekit raises bare Exception for bad sig / hash mismatch
        return Response(
            content=json.dumps({"error": "verification failed", "detail": str(exc)}),
            status_code=401,
            media_type="application/json",
        )

    room = event.room if event.HasField("room") else None
    participant = event.participant if event.HasField("participant") else None

    account_id = resolve_account_id(
        room_metadata=room.metadata if room else None,
        identities=[participant.identity] if participant and participant.identity else None,
    )

    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO events (ts, type, room_sid, room_name, account_id,
                                participant_id, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso(event.created_at or None),
                event.event,
                room.sid if room else None,
                room.name if room else None,
                account_id,
                participant.identity if participant else None,
                body,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Log a compact line so `python -m receiver.app` shows activity in the terminal.
    print(
        f"[webhook] {event.event:<32} room={room.name if room else '-'} "
        f"account={account_id or '-'} "
        f"participant={participant.identity if participant else '-'}"
    )

    return Response(status_code=200)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("RECEIVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("RECEIVER_PORT", "8080")),
    )
