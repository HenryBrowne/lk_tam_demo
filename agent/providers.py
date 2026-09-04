"""Swap-by-env provider factories for the STT -> LLM -> TTS pipeline.

Defaults: Deepgram STT, Claude LLM, Cartesia TTS. Set AGENT_STT_PROVIDER /
AGENT_LLM_PROVIDER / AGENT_TTS_PROVIDER to ``openai`` to move that stage to
OpenAI (the all-OpenAI, one-key fallback path). Plugins read their own API keys
from the environment (DEEPGRAM_API_KEY, CARTESIA_API_KEY, OPENAI_API_KEY,
ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import os


def _p(name: str, default: str) -> str:
    return os.environ.get(name, default).strip().lower()


def make_stt():
    provider = _p("AGENT_STT_PROVIDER", "deepgram")
    if provider == "openai":
        from livekit.plugins import openai

        return openai.STT(model="whisper-1")
    if provider == "deepgram":
        from livekit.plugins import deepgram

        return deepgram.STT(model="nova-3", language="en-US")
    raise ValueError(f"unknown AGENT_STT_PROVIDER={provider!r} (deepgram | openai)")


def make_llm():
    provider = _p("AGENT_LLM_PROVIDER", "anthropic")
    model = os.environ.get("AGENT_LLM_MODEL", "claude-haiku-4-5")
    if provider == "anthropic":
        from livekit.plugins import anthropic

        # NOTE: livekit-plugins-anthropic 1.7.1 only suppresses assistant-prefill
        # for claude-sonnet-4-6 / claude-opus-4-6. claude-haiku-4-5 still accepts
        # prefill, so it is the safe default here. See NOTES-livekit-api.md section 6.
        return anthropic.LLM(model=model)
    if provider == "openai":
        from livekit.plugins import openai

        return openai.LLM(model=os.environ.get("AGENT_LLM_MODEL", "gpt-4o-mini"))
    raise ValueError(f"unknown AGENT_LLM_PROVIDER={provider!r} (anthropic | openai)")


def make_tts():
    provider = _p("AGENT_TTS_PROVIDER", "cartesia")
    if provider == "openai":
        from livekit.plugins import openai

        return openai.TTS(model="tts-1", voice="alloy")
    if provider == "cartesia":
        from livekit.plugins import cartesia

        return cartesia.TTS(model="sonic-3")
    raise ValueError(f"unknown AGENT_TTS_PROVIDER={provider!r} (cartesia | openai)")


def describe() -> str:
    return (
        f"STT={_p('AGENT_STT_PROVIDER', 'deepgram')} "
        f"LLM={_p('AGENT_LLM_PROVIDER', 'anthropic')}:{os.environ.get('AGENT_LLM_MODEL', 'claude-haiku-4-5')} "
        f"TTS={_p('AGENT_TTS_PROVIDER', 'cartesia')}"
    )
