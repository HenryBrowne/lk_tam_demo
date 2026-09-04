"""Turn the agent's metrics_collected / connection_quality_changed events into
``turn_metrics`` and ``quality_events`` rows.

A "turn" is assembled from the separate STT/EOU/LLM/TTS metric objects that share
a ``speech_id`` (the agent's response-speech id). We buffer per speech_id and
write the row when the TTS metric arrives (reply audio has started = the turn is
complete enough to record). Anything still buffered at shutdown is flushed as-is.

All SDK latency fields are in seconds; we store milliseconds.
"""

from __future__ import annotations

import logging
import sqlite3

from livekit.agents.metrics import EOUMetrics, LLMMetrics, STTMetrics, TTSMetrics

from pipeline.db import iso

logger = logging.getLogger("portfolio-signal-agent")

_QUALITY_MAP = {
    "QUALITY_EXCELLENT": "excellent",
    "QUALITY_GOOD": "good",
    "QUALITY_POOR": "poor",
    "QUALITY_LOST": "lost",
    "QUALITY_UNKNOWN": "unknown",
}


def _ms(seconds: float | None) -> float | None:
    if seconds is None or seconds < 0:  # SDK uses -1 for "no token generated"
        return None
    return round(seconds * 1000.0, 1)


class MetricsSink:
    def __init__(self, conn: sqlite3.Connection, state: dict):
        # state is a mutable dict owned by the entrypoint: {"room_sid", "account_id"}
        self._conn = conn
        self._state = state
        self._turns: dict[str, dict] = {}

    # ---- metrics_collected -------------------------------------------------
    def handle_metric(self, m) -> None:
        if isinstance(m, EOUMetrics):
            turn = self._turn(m.speech_id)
            turn["eou_ms"] = _ms(m.end_of_utterance_delay)
            turn["transcription_ms"] = _ms(m.transcription_delay)
        elif isinstance(m, LLMMetrics):
            turn = self._turn(m.speech_id)
            turn["ttft_ms"] = _ms(m.ttft)
            turn["llm_tokens_in"] = m.prompt_tokens
            turn["llm_tokens_out"] = m.completion_tokens
            if m.cancelled:
                turn["error_flag"] = 1
        elif isinstance(m, TTSMetrics):
            turn = self._turn(m.speech_id)
            turn["ttfb_ms"] = _ms(m.ttfb)
            if m.cancelled:
                turn["error_flag"] = 1
            self._flush(m.speech_id)  # TTS is last in the chain
        elif isinstance(m, STTMetrics):
            # STT metrics have no speech_id, so they can't be tied to a turn here.
            # Token/usage totals come from session_usage_updated instead.
            pass

    def _turn(self, speech_id: str | None) -> dict:
        key = speech_id or "_no_speech_id"
        return self._turns.setdefault(
            key,
            {
                "ttft_ms": None, "ttfb_ms": None, "eou_ms": None,
                "transcription_ms": None, "llm_tokens_in": None,
                "llm_tokens_out": None, "error_flag": 0,
            },
        )

    def _flush(self, speech_id: str | None) -> None:
        key = speech_id or "_no_speech_id"
        turn = self._turns.pop(key, None)
        if turn is None:
            return
        parts = [
            turn["eou_ms"], turn["transcription_ms"], turn["ttft_ms"], turn["ttfb_ms"],
        ]
        total = round(sum(p for p in parts if p is not None), 1) if any(
            p is not None for p in parts
        ) else None
        self._conn.execute(
            """
            INSERT INTO turn_metrics (ts, room_sid, account_id, speech_id, ttft_ms,
                                      ttfb_ms, eou_ms, total_latency_ms, llm_tokens_in,
                                      llm_tokens_out, error_flag, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live')
            """,
            (
                iso(), self._state.get("room_sid"), self._state.get("account_id"),
                speech_id, turn["ttft_ms"], turn["ttfb_ms"], turn["eou_ms"], total,
                turn["llm_tokens_in"], turn["llm_tokens_out"], turn["error_flag"],
            ),
        )
        self._conn.commit()
        logger.info(
            "turn recorded speech_id=%s ttft=%s ttfb=%s eou=%s total=%s tok_in=%s tok_out=%s",
            speech_id, turn["ttft_ms"], turn["ttfb_ms"], turn["eou_ms"], total,
            turn["llm_tokens_in"], turn["llm_tokens_out"],
        )

    def flush_all(self) -> None:
        for speech_id in list(self._turns):
            self._flush(speech_id)

    # ---- connection_quality_changed --------------------------------------
    def handle_quality(self, participant, quality) -> None:
        label = _QUALITY_MAP.get(getattr(quality, "name", str(quality)), "unknown")
        self._conn.execute(
            """
            INSERT INTO quality_events (ts, room_sid, account_id, participant_id, quality, source)
            VALUES (?, ?, ?, ?, ?, 'live')
            """,
            (
                iso(), self._state.get("room_sid"), self._state.get("account_id"),
                getattr(participant, "identity", None), label,
            ),
        )
        self._conn.commit()
        logger.info(
            "quality %s for participant=%s",
            label, getattr(participant, "identity", None),
        )
