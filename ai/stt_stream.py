"""Streaming STT via Deepgram live transcription.

Audio frames come from any AudioSource (local mic now, Twilio later).
Interim transcripts are yielded as they arrive so the UI can show live text;
a complete user utterance is marked `speech_final` / UtteranceEnd.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import websockets

from ai.audio_io import AudioSource
from ai.config import (
    deepgram_api_key,
    deepgram_model,
    stt_endpointing_ms,
    stt_language,
    stt_sample_rate,
)

_DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen"
_KEEPALIVE_SECONDS = 5.0


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    speech_final: bool
    utterance_end: bool = False
    confidence: float = 0.0


def _ws_connect(url: str, headers: dict[str, str]):
    try:
        return websockets.connect(url, additional_headers=headers, max_size=None)
    except TypeError:
        return websockets.connect(url, extra_headers=headers, max_size=None)


async def stream_stt(
    source: AudioSource,
    *,
    api_key: str | None = None,
    model: str | None = None,
    language: str | None = None,
    sample_rate: int | None = None,
) -> AsyncIterator[TranscriptEvent]:
    """Yield Deepgram transcript events for PCM frames from `source`."""
    rate = sample_rate or getattr(source, "sample_rate", None) or stt_sample_rate()
    params = urlencode(
        {
            "model": model or deepgram_model(),
            "language": language or stt_language(),
            "encoding": "linear16",
            "sample_rate": rate,
            "channels": 1,
            "punctuate": "true",
            "interim_results": "true",
            "endpointing": stt_endpointing_ms(),
            "utterance_end_ms": "1200",
            "smart_format": "true",
            "vad_events": "true",
        }
    )
    url = f"{_DEEPGRAM_URL}?{params}"
    headers = {"Authorization": f"Token {api_key or deepgram_api_key()}"}

    async with _ws_connect(url, headers) as ws:
        stop = asyncio.Event()
        sender = asyncio.create_task(_send_audio(ws, source, stop))
        keepalive = asyncio.create_task(_keepalive(ws, stop))
        try:
            async for raw in ws:
                if stop.is_set():
                    break
                event = _parse_message(raw)
                if event is not None:
                    yield event
        finally:
            stop.set()
            sender.cancel()
            keepalive.cancel()
            for task in (sender, keepalive):
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


async def iter_final_utterances(
    source: AudioSource,
    *,
    on_partial: Callable[[str], None] | None = None,
    api_key: str | None = None,
    model: str | None = None,
    language: str | None = None,
    sample_rate: int | None = None,
) -> AsyncIterator[str]:
    """Yield a complete user utterance once Deepgram decides the speaker paused."""
    pending: list[str] = []
    async for event in stream_stt(
        source,
        api_key=api_key,
        model=model,
        language=language,
        sample_rate=sample_rate,
    ):
        if event.utterance_end:
            text = " ".join(pending).strip()
            pending.clear()
            if text:
                yield text
            continue
        if not event.text:
            continue
        if event.is_final:
            pending.append(event.text)
            if event.speech_final:
                text = " ".join(pending).strip()
                pending.clear()
                if text:
                    yield text
            continue
        if on_partial is not None:
            preview = " ".join([*pending, event.text]).strip()
            if preview:
                on_partial(preview)


async def _send_audio(
    ws: object,
    source: AudioSource,
    stop: asyncio.Event,
) -> None:
    try:
        async for frame in _iter_frames(source):
            if stop.is_set():
                break
            if frame:
                await ws.send(frame)  # type: ignore[union-attr]
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))  # type: ignore[union-attr]
        except Exception:
            pass
    except asyncio.CancelledError:
        raise
    except Exception:
        stop.set()
        raise


async def _iter_frames(source: AudioSource) -> AsyncIterator[bytes]:
    stream = source.frames()
    if hasattr(stream, "__aiter__"):
        async for frame in stream:  # type: ignore[union-attr]
            yield frame
        return
    for frame in stream:  # type: ignore[union-attr]
        yield frame


async def _keepalive(ws: object, stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            await asyncio.sleep(_KEEPALIVE_SECONDS)
            if stop.is_set():
                break
            try:
                await ws.send(json.dumps({"type": "KeepAlive"}))  # type: ignore[union-attr]
            except Exception:
                break
    except asyncio.CancelledError:
        return


def _parse_message(raw: str | bytes) -> TranscriptEvent | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None

    msg_type = payload.get("type")
    if msg_type == "UtteranceEnd":
        return TranscriptEvent(text="", is_final=True, speech_final=True, utterance_end=True)
    if msg_type == "Error":
        message = payload.get("message") or payload.get("description") or raw
        raise RuntimeError(f"Deepgram STT failed: {message}")
    if msg_type not in {None, "Results"}:
        return None

    channel = payload.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    if not alternatives:
        return None
    alt = alternatives[0]
    text = (alt.get("transcript") or "").strip()
    if not text and not payload.get("is_final"):
        return None
    return TranscriptEvent(
        text=text,
        is_final=bool(payload.get("is_final")),
        speech_final=bool(payload.get("speech_final")),
        confidence=float(alt.get("confidence") or 0.0),
    )
