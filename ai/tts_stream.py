"""ElevenLabs streaming TTS.

Each completed sentence is sent immediately. Audio bytes are yielded as they
arrive so playback (or Twilio) can start before the sentence is fully synthesized.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx

from ai.config import (
    elevenlabs_api_key,
    elevenlabs_model_id,
    elevenlabs_output_format,
    elevenlabs_voice_id,
)
from ai.scenarios.types import Scenario

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"


async def stream_tts(
    text: str,
    *,
    scenario: Scenario | None = None,
    voice_id: str | None = None,
    model_id: str | None = None,
    output_format: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[bytes]:
    """Yield raw audio chunks for a single sentence.

    Default format is PCM 24 kHz 16-bit mono. Swap `output_format` to
    `ulaw_8000` when this sink is replaced with Twilio Media Streams.
    """
    spoken = text.strip()
    if not spoken:
        return

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    try:
        async for chunk in _request_stream(
            http,
            spoken,
            voice_id=voice_id or (scenario.tts_voice_id if scenario else None) or elevenlabs_voice_id(),
            model_id=model_id or elevenlabs_model_id(),
            output_format=output_format or elevenlabs_output_format(),
            # 0.74/0.72 used to be the no-scenario fallback, which contradicts
            # ai/voices.py's own guidance ("lower stability ~0.35-0.45 sounds more
            # human; 0.7+ sounds like a robot"). In real calls a scenario is always
            # passed so this rarely fires, but it should agree with that guidance
            # instead of quietly shipping the robotic-sounding defaults.
            stability=scenario.tts_stability if scenario else 0.35,
            similarity_boost=scenario.tts_similarity_boost if scenario else 0.4,
            style=scenario.tts_style if scenario else 0.15,
            speed=scenario.tts_speed if scenario else 0.94,
        ):
            yield chunk
    finally:
        if owns_client:
            await http.aclose()


async def _request_stream(
    http: httpx.AsyncClient,
    text: str,
    *,
    voice_id: str,
    model_id: str,
    output_format: str,
    stability: float,
    similarity_boost: float,
    style: float,
    speed: float,
) -> AsyncIterator[bytes]:
    url = _TTS_URL.format(voice_id=voice_id)
    headers = {
        "xi-api-key": elevenlabs_api_key(),
        "Content-Type": "application/json",
        "Accept": "application/octet-stream",
    }
    params = {"output_format": output_format}
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            # style/use_speaker_boost were previously left at ElevenLabs'
            # defaults (0 / off), which reads as a flat script. speaker_boost
            # keeps articulation clear at the low stability we use.
            "style": style,
            "use_speaker_boost": True,
            "speed": speed,
        },
    }

    async with http.stream("POST", url, headers=headers, params=params, json=payload) as response:
        if response.status_code >= 400:
            body = (await response.aread()).decode("utf-8", errors="replace")
            hint = ""
            if response.status_code == 402:
                hint = (
                    " Free-plan accounts cannot use Voice Library voices. "
                    "Set ELEVENLABS_VOICE_ID to a premade voice "
                    "(e.g. Adam pNInz6obpgDQGcFmaJgB)."
                )
            raise RuntimeError(
                f"ElevenLabs TTS failed ({response.status_code}): {body[:400]}{hint}"
            )
        async for chunk in response.aiter_bytes(chunk_size=4096):
            if chunk:
                yield chunk
