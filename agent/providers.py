"""Swap-by-env provider factories for the STT -> LLM -> TTS pipeline.

Defaults: Deepgram STT, Claude LLM, Cartesia TTS. Set AGENT_STT_PROVIDER /
AGENT_LLM_PROVIDER / AGENT_TTS_PROVIDER to ``openai`` to move that stage to
OpenAI (the all-OpenAI, one-key fallback path). Plugins read their own API keys
from the environment (DEEPGRAM_API_KEY, CARTESIA_API_KEY, OPENAI_API_KEY,
ANTHROPIC_API_KEY).

NOTE: the plugin packages are imported at module load, not lazily inside the
factories. livekit-agents requires ``Plugin.register_plugin()`` to run on the
main thread, and the factories are called from a job thread.
"""

from __future__ import annotations

import os

import anthropic as anthropic_sdk
from livekit.plugins import anthropic, cartesia, deepgram, openai


def _p(name: str, default: str) -> str:
    return os.environ.get(name, default).strip().lower()


def make_stt():
    provider = _p("AGENT_STT_PROVIDER", "deepgram")
    if provider == "deepgram":
        return deepgram.STT(model="nova-3", language="en-US")
    if provider == "openai":
        return openai.STT(model="whisper-1")
    raise ValueError(f"unknown AGENT_STT_PROVIDER={provider!r} (deepgram | openai)")


def make_llm():
    provider = _p("AGENT_LLM_PROVIDER", "anthropic")
    if provider == "anthropic":
        # Two livekit-plugins-anthropic 1.7.1 quirks, both handled here (see
        # NOTES-livekit-api.md section 6):
        #  1. It builds an httpx(v1) client, which the httpx2-based anthropic SDK
        #     rejects with a TypeError. Passing our own AsyncAnthropic (reads
        #     ANTHROPIC_API_KEY from env) bypasses the plugin's client wiring.
        #  2. Its prefill-suppression list only covers claude-sonnet-4-6 /
        #     claude-opus-4-6; claude-haiku-4-5 still accepts prefill, so it is the
        #     safe default model.
        return anthropic.LLM(
            model=os.environ.get("AGENT_LLM_MODEL", "claude-haiku-4-5"),
            client=anthropic_sdk.AsyncAnthropic(),
        )
    if provider == "openai":
        return openai.LLM(model=os.environ.get("AGENT_LLM_MODEL", "gpt-4o-mini"))
    raise ValueError(f"unknown AGENT_LLM_PROVIDER={provider!r} (anthropic | openai)")


def make_tts():
    provider = _p("AGENT_TTS_PROVIDER", "cartesia")
    if provider == "cartesia":
        return cartesia.TTS(model="sonic-3")
    if provider == "openai":
        return openai.TTS(model="tts-1", voice="alloy")
    raise ValueError(f"unknown AGENT_TTS_PROVIDER={provider!r} (cartesia | openai)")


def describe() -> str:
    return (
        f"STT={_p('AGENT_STT_PROVIDER', 'deepgram')} "
        f"LLM={_p('AGENT_LLM_PROVIDER', 'anthropic')}:{os.environ.get('AGENT_LLM_MODEL', 'claude-haiku-4-5')} "
        f"TTS={_p('AGENT_TTS_PROVIDER', 'cartesia')}"
    )
