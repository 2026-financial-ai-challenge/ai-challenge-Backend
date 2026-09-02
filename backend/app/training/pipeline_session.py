"""PipelineSession tweaks for phone greeting lock, short turns, and hang-up."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import logging
import re
from collections.abc import AsyncIterator

from clawops.agent._audio import pcm16_to_ulaw, resample_pcm16
from clawops.agent.pipeline import PipelineSession, SpeechEvent


log = logging.getLogger("clawops.agent.pipeline")

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[.?!。！？,，]")
_DIGIT_NOISE = re.compile(r"^[0-9\s]+$")
_HANG_UP = re.compile(
    r"(끊겠|끊을게|끊을게요|끊습니다|전화 끊|나중에 걸|그만하세요|그만 전화)"
)


def compact_speech(text: str) -> str:
    return _PUNCT.sub("", _SPACE.sub("", text or ""))


def is_echo(user_text: str, assistant_text: str) -> bool:
    user = compact_speech(user_text)
    assistant = compact_speech(assistant_text)
    if len(user) < 2 or not assistant:
        return False
    if user in assistant or assistant.startswith(user):
        return True
    if len(user) < 8:
        return False
    segments = [compact_speech(part) for part in _PUNCT.split(assistant_text)]
    return any(
        len(segment) >= 8
        and SequenceMatcher(None, user, segment, autojunk=False).ratio() >= 0.72
        for segment in segments
    )


def is_garbage_transcript(text: str) -> bool:
    tokens = (text or "").split()
    if len(tokens) >= 8 and len(set(tokens)) <= 2 and all(len(tok) <= 2 for tok in tokens):
        return True
    compact = compact_speech(text)
    return bool(compact) and _DIGIT_NOISE.fullmatch(text or "") is not None and len(compact) >= 8


def wants_hang_up(text: str) -> bool:
    return bool(_HANG_UP.search(text or ""))


def should_hang_up_now(*, hangup_attempts: int, user_turns: int, max_turns: int) -> bool:
    if user_turns >= max_turns:
        return True
    return hangup_attempts >= 2


def greeting_playback_seconds(text: str, *, default: float = 8.0) -> float:
    """Estimate how long the opening line takes to play on the PSTN path."""
    compact = compact_speech(text)
    if not compact:
        return default
    return max(5.0, min(12.0, len(compact) / 7.0 + 1.5))


# Media websocket connects after attach() returns. Speaking into the
# prewarm buffer dumps the whole greeting as a burst when the callee
# answers, which warps Korean TTS into a drawn-out "오오오".
_ANSWER_SETTLE_SECONDS = 0.8
_FALLBACK_OPENING = "안녕하세요."


class PhonePipelineSession(PipelineSession):
    """Fixed opening line after answer, greeting lock, then short scripted turns."""

    def __init__(
        self,
        *,
        stt,
        llm,
        tts,
        system_prompt: str = "",
        greeting: bool = True,
        language: str = "ko",
        tool_registry=None,
        recorder=None,
        opening_line: str = "",
        max_turns: int = 6,
    ) -> None:
        super().__init__(
            stt=stt,
            llm=llm,
            tts=tts,
            system_prompt=system_prompt,
            greeting=greeting,
            language=language,
            tool_registry=tool_registry,
            recorder=recorder,
        )
        self._opening_line = opening_line.strip()
        self._max_turns = max(1, int(max_turns))
        self._user_turns = 0
        self._hangup_attempts = 0
        self._greeting_playing = bool(self._greeting)
        self._held_user_transcripts: list[str] = []
        self._greeting_unlock_task: asyncio.Task | None = None
        self._opening_task: asyncio.Task | None = None

    async def attach(self, call) -> None:
        await super().attach(call)
        if not self._greeting:
            self._greeting_playing = False
            return
        self._greeting_playing = True
        opening = self._opening_line or _FALLBACK_OPENING
        delay = _ANSWER_SETTLE_SECONDS + greeting_playback_seconds(opening)
        log.info("Greeting starts after answer; lock %.1fs", delay)
        if self._greeting_unlock_task and not self._greeting_unlock_task.done():
            self._greeting_unlock_task.cancel()
        if self._opening_task and not self._opening_task.done():
            self._opening_task.cancel()
        self._greeting_unlock_task = asyncio.create_task(self._unlock_greeting(delay))
        self._opening_task = asyncio.create_task(self._speak_opening_after_answer())

    async def stop(self) -> None:
        if self._greeting_unlock_task and not self._greeting_unlock_task.done():
            self._greeting_unlock_task.cancel()
        if self._opening_task and not self._opening_task.done():
            self._opening_task.cancel()
        await super().stop()

    async def _generate_greeting(self) -> None:
        # ClawOps prewarms during ring and would speak into a buffer that is
        # later flushed as a burst. Opening audio starts in attach() instead.
        return

    async def _speak_opening_after_answer(self) -> None:
        try:
            await asyncio.sleep(_ANSWER_SETTLE_SECONDS)
        except asyncio.CancelledError:
            return
        if not self._running or not self._call:
            return
        await self._speak_fixed(self._opening_line or _FALLBACK_OPENING)

    async def _unlock_greeting(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._finish_greeting()

    async def _finish_greeting(self) -> None:
        if not self._greeting_playing:
            return
        self._greeting_playing = False
        log.info("Greeting playback lock released")
        held = " ".join(self._held_user_transcripts).strip()
        self._held_user_transcripts.clear()
        if held:
            await self._handle_final_transcript(
                SpeechEvent(type="final", transcript=held)
            )

    async def _handle_interim_speech(self, event: SpeechEvent) -> None:
        text = (event.transcript or "").strip()
        if self._greeting_playing:
            log.info("Ignoring barge-in during greeting: %s", text[:30])
            return
        if len(text) < 2:
            return
        last = self._last_assistant_text()
        if last and is_echo(text, last):
            log.info("Ignoring echo barge-in: %s", text[:30])
            return
        await super()._handle_interim_speech(event)

    async def _handle_final_transcript(self, event: SpeechEvent) -> None:
        text = (event.transcript or "").strip()
        if not text:
            return
        if is_garbage_transcript(text):
            log.info("Ignoring garbage transcript: %s", text[:40])
            return
        if self._greeting_playing:
            log.info("Holding user transcript until greeting ends: %s", text[:40])
            self._held_user_transcripts.append(text)
            return
        last = self._last_assistant_text()
        if last and is_echo(text, last):
            log.info("Ignoring echo transcript: %s", text[:40])
            return

        log.info("STT: %s", text)
        self._user_turns += 1
        if wants_hang_up(text):
            self._hangup_attempts += 1
        if should_hang_up_now(
            hangup_attempts=self._hangup_attempts,
            user_turns=self._user_turns,
            max_turns=self._max_turns,
        ):
            self._messages.append({"role": "user", "content": text})
            await self._speak_fixed(
                "지금 끊으시면 그 삼십만 원 건이 그대로 넘어갑니다.",
                hang_up_after=True,
            )
            return
        await super()._handle_final_transcript(event)

    async def _speak_fixed(self, text: str, *, hang_up_after: bool = False) -> None:
        spoken = text.strip()
        if not spoken or not self._call:
            if hang_up_after:
                await self._hang_up()
            return
        try:

            async def sentences() -> AsyncIterator[str]:
                yield spoken

            tts_sample_rate = getattr(self._tts, "sample_rate", 24000)
            self._sent_audio_chunks = 0
            async for audio in self._tts.synthesize(sentences()):
                if not self._running or not self._call:
                    break
                pcm8k = resample_pcm16(audio, from_rate=tts_sample_rate, to_rate=8000)
                ulaw = pcm16_to_ulaw(pcm8k)
                if not self._first_audio_logged:
                    from clawops.agent.pipeline._buffering_call import (
                        log_first_realtime_audio,
                    )

                    log_first_realtime_audio(self._call)
                    self._first_audio_logged = True
                await self._call.send_audio(ulaw)
                self._sent_audio_chunks += 1
            log.info("Assistant: %s", spoken[:100])
            if self._call:
                await self._call._emit("transcript", "assistant", spoken)
            self._messages.append({"role": "assistant", "content": spoken})
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Fixed TTS error: %s", exc)
        finally:
            if hang_up_after:
                await self._hang_up()

    async def _hang_up(self) -> None:
        hangup = getattr(self._call, "hangup", None)
        if not callable(hangup):
            return
        log.info("Hanging up training call")
        try:
            await hangup()
        except Exception as exc:
            log.warning("hangup failed: %s", exc)

    def _last_assistant_text(self) -> str:
        for message in reversed(self._messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return ""
