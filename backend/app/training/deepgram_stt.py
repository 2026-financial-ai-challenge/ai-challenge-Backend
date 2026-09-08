"""Deepgram STT that flushes a turn even when speech_final arrives separately.

ClawOps' DeepgramSTT only emits a final event when one Results payload has
both is_final and speech_final and a non-empty transcript. Deepgram often
sends those on consecutive messages (text + empty speech_final, or
UtteranceEnd). The pipeline then never calls the LLM after barge-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

import aiohttp

from clawops.agent.pipeline import DeepgramSTT, SpeechEvent


log = logging.getLogger("clawops.agent.pipeline")

DEEPGRAM_WS_URL = "wss://api.deepgram.com/v1/listen"


class UtteranceAssembler:
    def __init__(self) -> None:
        self._pending: list[str] = []
        self.speech_notified = False

    def ingest(self, data: dict) -> list[tuple[str, str]]:
        msg_type = data.get("type", "")
        if msg_type == "SpeechStarted":
            # VAD-only: do not emit interim. Empty barge-in cuts TTS on speaker echo.
            return []
        if msg_type == "UtteranceEnd":
            return self._finish()
        if msg_type != "Results":
            return []

        is_final = bool(data.get("is_final", False))
        speech_final = bool(data.get("speech_final", False))
        transcript = _result_transcript(data)

        events: list[tuple[str, str]] = []
        if transcript and not is_final:
            events.extend(self._mark_speech(transcript))
        if transcript and is_final:
            self._pending.append(transcript)
        if speech_final:
            events.extend(self._finish())
        return events

    def flush(self) -> str:
        text = " ".join(part for part in self._pending if part).strip()
        self._pending.clear()
        return text

    def _mark_speech(self, transcript: str = "") -> list[tuple[str, str]]:
        if self.speech_notified:
            return []
        self.speech_notified = True
        return [("interim", transcript)]

    def _finish(self) -> list[tuple[str, str]]:
        text = self.flush()
        self.speech_notified = False
        if not text:
            return []
        return [("final", text)]


def _result_transcript(data: dict) -> str:
    channel = data.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    if not alternatives:
        return ""
    return str(alternatives[0].get("transcript") or "").strip()


class PhoneDeepgramSTT(DeepgramSTT):
    """Deepgram live STT with utterance assembly for phone barge-in."""

    async def transcribe(self, audio_stream: AsyncIterator[bytes]) -> AsyncIterator[SpeechEvent]:
        params = (
            f"model={self._model}&language={self._language}"
            f"&sample_rate={self._sample_rate}&encoding={self._encoding}"
            f"&channels=1&punctuate={str(self._punctuate).lower()}"
            f"&interim_results={str(self._interim_results).lower()}"
            f"&endpointing={self._endpointing}"
            f"&utterance_end_ms={self._utterance_end_ms}"
            f"&vad_events=true&smart_format=true"
        )
        url = f"{DEEPGRAM_WS_URL}?{params}"
        assembler = UtteranceAssembler()

        session = aiohttp.ClientSession()
        try:
            ws = await session.ws_connect(
                url,
                headers={"Authorization": f"Token {self._api_key}"},
            )
            log.info("Deepgram STT connected")

            event_queue: asyncio.Queue[SpeechEvent | None] = asyncio.Queue()

            async def send_audio() -> None:
                try:
                    async for chunk in audio_stream:
                        if ws.closed:
                            break
                        await ws.send_bytes(chunk)
                except Exception as e:
                    log.error("Deepgram send error: %s", e)
                finally:
                    if not ws.closed:
                        await ws.send_bytes(b"")

            async def recv_results() -> None:
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            for event_type, transcript in assembler.ingest(data):
                                if event_type == "interim":
                                    if transcript:
                                        log.info(
                                            "Speech detected (interim): %s",
                                            transcript[:40],
                                        )
                                    else:
                                        log.info("Speech started (VAD)")
                                else:
                                    log.info("STT final: %s", transcript[:80])
                                await event_queue.put(
                                    SpeechEvent(type=event_type, transcript=transcript)
                                )
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                except Exception as e:
                    log.error("Deepgram recv error: %s", e)
                finally:
                    leftover = assembler.flush()
                    if leftover:
                        log.info("STT final (stream end): %s", leftover[:80])
                        await event_queue.put(
                            SpeechEvent(type="final", transcript=leftover)
                        )
                    await event_queue.put(None)

            send_task = asyncio.create_task(send_audio())
            recv_task = asyncio.create_task(recv_results())
            try:
                while True:
                    event = await event_queue.get()
                    if event is None:
                        break
                    yield event
            finally:
                send_task.cancel()
                recv_task.cancel()
                if not ws.closed:
                    await ws.close()
        finally:
            await session.close()
