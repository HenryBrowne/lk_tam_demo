"""Synthetic caller: join a LiveKit room tagged to an account and speak a few
turns of Cartesia-synthesized speech, so the running agent produces real
turn_metrics / quality_events rows without a human at a mic.

Prereq: the agent worker must be running (`python -m agent.main dev`).

    python scripts/sim_call.py --account acme-corp
    python scripts/sim_call.py --account globex --gap 8 --turns 2

This is also the seed for Phase 3's simulate_accounts.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
import urllib.request

from dotenv import load_dotenv
from livekit import api, rtc

load_dotenv()

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_MS = 10
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000        # 160
BYTES_PER_FRAME = SAMPLES_PER_FRAME * 2                   # 320 (s16le mono)

# A short, coherent "prospect evaluating the platform" call.
DEFAULT_UTTERANCES = [
    "Hi there. I'm evaluating your platform for a customer support use case. "
    "In a sentence or two, what do you do?",
    "Got it. How does pricing work as call volume grows?",
    "Last thing - do you support phone numbers and SIP trunking?",
]

# Cartesia default English voice id (same one used in the Phase 2 key check).
CARTESIA_VOICE_ID = "a0e99841-438c-4a64-b679-ae501e7d6091"


def cartesia_pcm(text: str) -> bytes:
    """Synthesize `text` to raw 16 kHz mono s16le PCM via Cartesia's HTTP API."""
    body = json.dumps(
        {
            "model_id": "sonic-3",
            "transcript": text,
            "voice": {"mode": "id", "id": CARTESIA_VOICE_ID},
            "language": "en",
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": SAMPLE_RATE,
            },
        }
    ).encode()
    req = urllib.request.Request(
        "https://api.cartesia.ai/tts/bytes",
        data=body,
        method="POST",
        headers={
            "X-API-Key": os.environ["CARTESIA_API_KEY"],
            "Cartesia-Version": "2024-11-13",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


async def _push(source: rtc.AudioSource, pcm: bytes) -> None:
    """Feed PCM to the track one 10 ms frame at a time. capture_frame() blocks
    when the internal queue is full, which paces this at ~real time."""
    for i in range(0, len(pcm), BYTES_PER_FRAME):
        chunk = pcm[i : i + BYTES_PER_FRAME]
        if len(chunk) < BYTES_PER_FRAME:
            chunk = chunk + b"\x00" * (BYTES_PER_FRAME - len(chunk))
        await source.capture_frame(
            rtc.AudioFrame(chunk, SAMPLE_RATE, CHANNELS, SAMPLES_PER_FRAME)
        )


async def _silence(source: rtc.AudioSource, ms: int) -> None:
    await _push(source, b"\x00" * (BYTES_PER_FRAME * (ms // FRAME_MS)))


async def run(account: str, room_name: str, gap_s: float, n_turns: int) -> None:
    url = os.environ["LIVEKIT_URL"]
    key = os.environ["LIVEKIT_API_KEY"]
    secret = os.environ["LIVEKIT_API_SECRET"]

    # 1. create the room carrying the account tag
    lk = api.LiveKitAPI(url, key, secret)
    try:
        await lk.room.create_room(
            api.CreateRoomRequest(
                name=room_name,
                empty_timeout=120,
                metadata=json.dumps({"account_id": account}),
            )
        )
        print(f"room '{room_name}' created  (account_id={account})")
    except Exception as exc:  # already exists, etc. - not fatal
        print(f"create_room: {exc} (continuing)")
    finally:
        await lk.aclose()

    # 2. synthesize the caller's lines
    lines = DEFAULT_UTTERANCES[:n_turns]
    print(f"synthesizing {len(lines)} utterance(s) via Cartesia ...")
    pcms = [cartesia_pcm(t) for t in lines]

    # 3. join as the caller and publish a mic track
    identity = f"acct-{account}__sim-caller"
    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name("Sim Caller")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )
    room = rtc.Room()
    await room.connect(url, token)
    print(f"connected as {identity}")

    source = rtc.AudioSource(SAMPLE_RATE, CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("caller-mic", source)
    opts = rtc.TrackPublishOptions()
    opts.source = rtc.TrackSource.SOURCE_MICROPHONE
    await room.local_participant.publish_track(track, opts)

    # 4. wait for the agent to be dispatched into the room
    print("waiting for the agent to join ...")
    for _ in range(30):
        if room.remote_participants:
            print("  agent joined:", *[p.identity for p in room.remote_participants.values()])
            break
        await asyncio.sleep(1)
    else:
        print("  no agent after 30s - is `python -m agent.main dev` running?")

    await asyncio.sleep(3)  # let the agent deliver its greeting

    # 5. speak the turns, leaving a gap for the agent to answer each
    for i, pcm in enumerate(pcms, 1):
        secs = len(pcm) / 2 / SAMPLE_RATE
        print(f"turn {i}/{len(pcms)}: speaking {secs:.1f}s ...")
        await _silence(source, 300)
        await _push(source, pcm)
        await _silence(source, 500)
        await source.wait_for_playout()
        await asyncio.sleep(gap_s)

    await asyncio.sleep(2)
    await room.disconnect()      # graceful end
    print("caller disconnected (graceful). Check rows with:  python -m pipeline.db")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--account", default="acme-corp", help="synthetic account_id (default: acme-corp)")
    ap.add_argument("--room", default=None, help="room name (default: sim-<account>-<rand>)")
    ap.add_argument("--gap", type=float, default=7.0, help="seconds to wait after each turn (default: 7)")
    ap.add_argument("--turns", type=int, default=len(DEFAULT_UTTERANCES), help="number of turns (max 3)")
    args = ap.parse_args()

    room_name = args.room or f"sim-{args.account}-{uuid.uuid4().hex[:6]}"
    asyncio.run(run(args.account, room_name, args.gap, max(1, min(args.turns, len(DEFAULT_UTTERANCES)))))


if __name__ == "__main__":
    main()
