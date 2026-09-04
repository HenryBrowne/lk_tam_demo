"""Minimal LiveKit voice agent + telemetry capture for Portfolio Signal.

It runs a plain STT -> LLM -> TTS assistant and, as a side effect, writes
``turn_metrics`` and ``quality_events`` rows to the SQLite DB. That is the whole
point of the agent in this project — it is a telemetry source, not a product.

Run (from the repo root, with .env filled in):

    python -m agent.main dev        # connects to LIVEKIT_URL, waits for a room
    python -m agent.main console    # talk to it in the terminal, no LiveKit room

Then join the room from https://agents-playground.livekit.io (point it at your
project) or `lk room join --identity you --publish-mic <room>`.
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    JobProcess,
    MetricsCollectedEvent,
    WorkerOptions,
    cli,
    metrics,
)
from livekit.plugins import silero

from agent.metrics_sink import MetricsSink
from agent.providers import describe, make_llm, make_stt, make_tts
from pipeline.db import connect
from pipeline.tagging import resolve_account_id

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("portfolio-signal-agent")

SYSTEM_PROMPT = (
    "You are a concise, friendly voice assistant demoing a LiveKit pipeline. "
    "Keep replies to two or three sentences. If asked what you are, say you are a "
    "test agent for a telemetry dashboard project."
)


def prewarm(proc: JobProcess) -> None:
    # Load the VAD once per worker process, not once per call.
    proc.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    await ctx.connect()
    room_sid = await ctx.room.sid
    logger.info("joined room name=%s sid=%s  pipeline: %s",
                ctx.room.name, room_sid, describe())

    # account_id: room metadata first; refined from a participant identity if needed.
    state = {
        "room_sid": room_sid,
        "account_id": resolve_account_id(ctx.room.metadata, []),
    }
    conn = connect()
    sink = MetricsSink(conn, state)

    session = AgentSession(
        stt=make_stt(),
        llm=make_llm(),
        tts=make_tts(),
        vad=ctx.proc.userdata["vad"],
        # turn detection is left to AgentSession's default (LiveKit inference when
        # hosted, local v1-mini model otherwise) — EOUMetrics fires either way.
    )

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)          # readable line in the worker log
        sink.handle_metric(ev.metrics)           # -> turn_metrics

    @ctx.room.on("connection_quality_changed")
    def _on_quality(participant, quality) -> None:
        sink.handle_quality(participant, quality)  # -> quality_events

    @ctx.room.on("participant_connected")
    def _on_participant(participant) -> None:
        if not state["account_id"]:
            resolved = resolve_account_id(ctx.room.metadata, [participant.identity])
            if resolved:
                state["account_id"] = resolved
                logger.info("account_id resolved from identity -> %s", resolved)

    async def _on_shutdown() -> None:
        sink.flush_all()
        conn.close()
        logger.info("metrics sink flushed and DB closed")

    ctx.add_shutdown_callback(_on_shutdown)

    await session.start(agent=Agent(instructions=SYSTEM_PROMPT), room=ctx.room, record=False)
    await session.generate_reply(
        instructions="Greet the caller in one sentence and ask how you can help."
    )


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
