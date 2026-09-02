"""HTTP ElevenLabs TTS for phone calls.

ClawOps' ElevenLabsTTS uses the websocket API: it opens the stream with a
lone space, never sends flush, and drops style/speed. On Korean that often
stretches the last vowel ("받으세요오오오"). One HTTP request per sentence
avoids that, and keeps the voice settings we actually tuned.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator

import httpx
from clawops.agent.pipeline import ElevenLabsTTS


log = logging.getLogger("clawops.agent.pipeline")

_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
_WAVY = re.compile(r"[~～]+")
_ELLIPSIS = re.compile(r"\.{2,}|…+")
_STRETCHED = re.compile(r"([가-힣])\1+")
_YO_STRETCH = re.compile(r"요오+$")
# 24 kHz → 8 kHz is 3:1, so keep a multiple of 3 samples (6 bytes).
_PCM16_ALIGN = 6


def sanitize_tts_text(text: str) -> str:
    """Strip TTS-only artifacts that make Korean vowels drone."""
    spoken = _WAVY.sub("", text or "")
    spoken = _ELLIPSIS.sub(".", spoken)
    spoken = _STRETCHED.sub(r"\1", spoken)
    spoken = _YO_STRETCH.sub("요", spoken)
    return spoken.strip()


def strip_wav_header(payload: bytes) -> bytes:
    """Drop a RIFF/WAVE wrapper so the header is not played as PCM."""
    if len(payload) < 44 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        return payload
    offset = 12
    while offset + 8 <= len(payload):
        chunk_id = payload[offset : offset + 4]
        chunk_size = int.from_bytes(payload[offset + 4 : offset + 8], "little")
        offset += 8
        if chunk_id == b"data":
            end = offset + chunk_size if chunk_size else len(payload)
            return payload[offset:end]
        offset += chunk_size + (chunk_size % 2)
    return payload


def align_pcm16(payload: bytes) -> bytes:
    """Keep complete 16-bit frames that resample 24 kHz → 8 kHz cleanly."""
    pcm = strip_wav_header(payload)
    n = len(pcm) - (len(pcm) % _PCM16_ALIGN)
    return pcm[:n]


class PhoneElevenLabsTTS(ElevenLabsTTS):
    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice_id: str = "EXAVITQu4vr4xnSDxMaL",
        model: str = "eleven_turbo_v2_5",
        output_format: str = "pcm_24000",
        stability: float = 0.5,
        similarity_boost: float = 0.75,
        style: float = 0.15,
        speed: float = 0.94,
        use_speaker_boost: bool = True,
        language_code: str = "ko",
    ) -> None:
        super().__init__(
            api_key=api_key,
            voice_id=voice_id,
            model=model,
            output_format=output_format,
            stability=stability,
            similarity_boost=similarity_boost,
            language_code=language_code,
        )
        self._style = style
        self._speed = speed
        self._use_speaker_boost = use_speaker_boost

    async def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[bytes]:
        timeout = httpx.Timeout(30.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            async for text in text_stream:
                spoken = sanitize_tts_text(text)
                if not spoken:
                    continue
                log.info("ElevenLabs HTTP TTS: %s", spoken[:60])
                async for chunk in self._stream_sentence(http, spoken):
                    yield chunk

    async def _stream_sentence(
        self, http: httpx.AsyncClient, text: str
    ) -> AsyncIterator[bytes]:
        url = _TTS_URL.format(voice_id=self._voice_id)
        headers = {
            "xi-api-key": self._api_key or os.getenv("ELEVENLABS_API_KEY", ""),
            "Content-Type": "application/json",
            "Accept": "application/octet-stream",
        }
        payload = {
            "text": text,
            "model_id": self._model,
            "language_code": self._language_code,
            "voice_settings": {
                "stability": self._stability,
                "similarity_boost": self._similarity_boost,
                "style": self._style,
                "use_speaker_boost": self._use_speaker_boost,
                "speed": self._speed,
            },
        }
        async with http.stream(
            "POST",
            url,
            headers=headers,
            params={"output_format": self._output_format},
            json=payload,
        ) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"ElevenLabs TTS failed ({response.status_code}): {body[:400]}"
                )
            # Collect the sentence first. Streaming odd-sized HTTP chunks
            # through 16-bit resample plays the header/remainder as a burst
            # of fake syllables before the real 안녕하세요.
            raw = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=4096):
                if chunk:
                    raw.extend(chunk)
            pcm = align_pcm16(bytes(raw))
            frame = 960  # 20 ms at 24 kHz PCM16, divisible by 24k→8k
            for offset in range(0, len(pcm), frame):
                piece = pcm[offset : offset + frame]
                aligned = align_pcm16(piece) if len(piece) < frame else piece
                if aligned:
                    yield aligned
