"""PipelineSession tweaks for phone greeting lock, short turns, and hang-up."""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import logging
import os
import re
from collections.abc import AsyncIterator

from clawops.agent._audio import pcm16_to_ulaw, resample_pcm16
from clawops.agent.pipeline import PipelineSession, SpeechEvent

from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()
from ai.hangup import wants_hang_up  # noqa: E402
from ai.scenarios.reflex import ReflexTable  # noqa: E402


log = logging.getLogger("clawops.agent.pipeline")

_SPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[.?!。！？,，]")
_DIGIT_NOISE = re.compile(r"^[0-9\s]+$")
# Used only when a scenario ships no hangup_line of its own. The old
# hardcoded line quoted one scenario's amount ("삼십만 원"), which was wrong
# for every other scenario in the library.
_DEFAULT_HANGUP_LINE = "지금 끊으시면 이 건은 그대로 넘어갑니다."
_DEFAULT_STALL_LINE = "여보세요. 확인 중이니 잠시만 기다려 주십시오."


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


def should_hang_up_now(*, hangup_attempts: int, user_turns: int, max_turns: int) -> bool:
    if user_turns >= max_turns:
        return True
    return hangup_attempts >= 2


# A long answer does not arrive as one final. Deepgram closes a final after
# `endpointing` milliseconds of silence -- 400 by default, see
# call_service.build_pipeline_session -- and a trainee thinking out loud
# pauses for longer than that mid-sentence. One breath therefore lands as
# three or four finals, and counting each of them as a turn spent the whole
# `max_turns` budget on a single answer and hung the call up mid-sentence.
#
# How long after a final the trainee may resume and still be finishing the
# same thought. Measured from the final, which Deepgram already emits 400ms
# into the silence, so this covers a real pause of roughly 1.4 seconds.
TURN_PAUSE_SECONDS = float(os.getenv("CALL_TURN_PAUSE_SEC", "1.0"))
# Only bound for a pipeline that has stopped answering altogether: without it,
# merging forever would keep `user_turns` from ever reaching `max_turns`.
MAX_TURN_FRAGMENTS = 6


def continues_previous_turn(
    messages: list[dict],
    *,
    fragments: int,
    pause_seconds: float | None,
) -> bool:
    """Is this final the rest of the previous one rather than a new turn?

    Two independent signals, because either one alone means the trainee
    cannot have been answering a reply of ours:

    - The trailing message is still theirs, so we never committed an answer
      to it. Barge-in cancelling our response is exactly what leaves the
      history in that state.
    - They resumed within `TURN_PAUSE_SECONDS`. Nothing we could say would
      have reached them and been answered inside that window, whatever the
      history looks like.

    `pause_seconds` is None when no speech-start reading exists for this
    utterance (Deepgram sent no usable interim); only the history is consulted
    then.
    """
    if fragments >= MAX_TURN_FRAGMENTS:
        return False
    if pause_seconds is not None and pause_seconds <= TURN_PAUSE_SECONDS:
        return True
    last = messages[-1] if messages else None
    if not last or last.get("role") != "user":
        return False
    return bool(str(last.get("content") or "").strip())


# 8 kHz mu-law is one byte per sample, so bytes sent == playback seconds * 8000.
_ULAW_BYTES_PER_SECOND = 8000
# Guard against a bad byte count parking the call on a very long sleep.
_MAX_PLAYBACK_WAIT_SECONDS = 30.0
# How long the caller gets to state who they are before the trainee can cut
# in. Real calls are interruptible, and holding every word for the whole
# 13-second opening was the least lifelike thing in the flow -- a real scammer
# answers "누구세요?" instead of talking over it for ten more seconds. This
# only protects the identifying first breath so the scenario's setup lands;
# after it, barge-in behaves like any other turn.
GREETING_GUARD_SECONDS = float(os.getenv("CALL_GREETING_GUARD_SEC", "3.0"))


def _remaining_playback_seconds(sent_bytes: int, started: float, now: float) -> float:
    """How much of what we sent the phone has not played yet.

    send_audio() returns as soon as the engine accepts the bytes, but the
    caller hears them at 8 kHz for seconds afterwards. Anything that ends a
    turn -- hanging up, releasing the barge-in lock -- has to wait this out or
    it cuts the line off mid-sentence.
    """
    if sent_bytes <= 0:
        return 0.0
    remaining = sent_bytes / _ULAW_BYTES_PER_SECOND - (now - started)
    return min(max(remaining, 0.0), _MAX_PLAYBACK_WAIT_SECONDS)


class PhonePipelineSession(PipelineSession):
    """Fixed opening line, greeting lock, then short scripted turns."""

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
        quick_replies: tuple[tuple[str, str], ...] = (),
        reflex_budget: int = 3,
        hangup_line: str = "",
        stall_line: str = "",
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
        # Turn continuation bookkeeping: how many extra finals the current
        # trainee turn has already absorbed, and the clock readings that say
        # how long they stayed quiet between the two.
        self._turn_fragments = 0
        self._last_final_at: float | None = None
        self._speech_started_at: float | None = None
        self._greeting_playing = bool(self._greeting)
        self._held_user_transcripts: list[str] = []
        self._greeting_unlock_task: asyncio.Task | None = None
        self._hangup_line = hangup_line.strip() or _DEFAULT_HANGUP_LINE
        self._reflexes = ReflexTable(quick_replies, budget=reflex_budget)
        # What we are saying right now. _messages only gets the line after the
        # whole thing has been sent, so without this the echo filter has
        # nothing to compare against during the seconds it matters most.
        self._speaking_text = ""
        # Said when the LLM turn produces nothing at all. Prefer the scenario's
        # own "say that again" line: it restates the fixed event, so it fits
        # anywhere in the call. Never leave the trainee listening to silence.
        self._stall_line = (
            stall_line.strip()
            or dict(quick_replies or ()).get("repeat_that", "")
            or _DEFAULT_STALL_LINE
        )

    async def attach(self, call) -> None:
        await super().attach(call)
        if not self._greeting_playing:
            return
        delay = GREETING_GUARD_SECONDS
        log.info("Greeting guard %.1fs after attach", delay)
        if self._greeting_unlock_task and not self._greeting_unlock_task.done():
            self._greeting_unlock_task.cancel()
        self._greeting_unlock_task = asyncio.create_task(self._unlock_greeting(delay))

    async def stop(self) -> None:
        if self._greeting_unlock_task and not self._greeting_unlock_task.done():
            self._greeting_unlock_task.cancel()
        await super().stop()

    async def _generate_greeting(self) -> None:
        await asyncio.sleep(0.5)
        if self._opening_line:
            self._current_response_task = asyncio.create_task(
                self._speak_fixed(self._opening_line)
            )
            return
        await super()._generate_greeting()

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
        log.info("Greeting guard released; barge-in is live")
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
        last = self._echo_reference()
        if last and is_echo(text, last):
            log.info("Ignoring echo barge-in: %s", text[:30])
            return
        # Taken here rather than on the final: the gap between two finals also
        # contains however long the trainee spoke, so only the moment their
        # voice comes back measures the pause.
        self._speech_started_at = asyncio.get_running_loop().time()
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
        last = self._echo_reference()
        if last and is_echo(text, last):
            log.info("Ignoring echo transcript: %s", text[:40])
            return

        log.info("STT: %s", text)
        # Read the new words only. A closing phrase from an earlier fragment
        # was already counted, and re-reading it after a merge would spend the
        # trainee's second hang-up attempt on their first one.
        if wants_hang_up(text):
            self._hangup_attempts += 1

        if continues_previous_turn(
            self._messages,
            fragments=self._turn_fragments,
            pause_seconds=self._pause_before_this_utterance(),
        ):
            self._turn_fragments += 1
            text = self._absorb_unanswered_turn(text)
            log.info(
                "Same trainee turn (fragment %s): %s",
                self._turn_fragments + 1,
                text[:60],
            )
            event = SpeechEvent(type="final", transcript=text)
        else:
            self._turn_fragments = 0
            self._user_turns += 1
        self._last_final_at = asyncio.get_running_loop().time()

        if should_hang_up_now(
            hangup_attempts=self._hangup_attempts,
            user_turns=self._user_turns,
            max_turns=self._max_turns,
        ):
            self._messages.append({"role": "user", "content": text})
            await self._speak_fixed(self._hangup_line, hang_up_after=True)
            return

        # Fast path: a handful of trainee lines ("안 들려요", "누구세요?") have
        # an answer the scenario already fixes, so answering from the table
        # skips the whole LLM round trip before the first TTS byte. Budgeted
        # and one-shot per trigger, so the call stays LLM-driven.
        reflex = self._reflexes.take(text)
        if reflex:
            log.info("Reflex reply (no LLM): %s", reflex[:40])
            self._messages.append({"role": "user", "content": text})
            await self._speak_fixed(reflex)
            return

        await super()._handle_final_transcript(event)

    async def _respond(self) -> None:
        """Speak something even when the LLM turn falls over.

        clawops' _respond swallows every exception and returns without
        sending audio, which on a phone call is dead air rather than a
        visible error. When the turn produced no audio and added no message,
        say the stall line so the caller stays in character.
        """
        turn_marker = len(self._messages)
        await super()._respond()

        if not (self._running and self._call):
            return
        if self._sent_audio_chunks > 0:
            return  # the model did answer
        if len(self._messages) != turn_marker:
            return  # a tool call (e.g. hang_up) or a newer turn moved on
        task = asyncio.current_task()
        if task is not None and getattr(task, "cancelling", lambda: 0)():
            return  # barge-in cancelled this turn; the next one will answer

        log.warning("LLM turn produced no audio; speaking the stall line")
        await self._speak_fixed(self._stall_line)

    async def _speak_fixed(self, text: str, *, hang_up_after: bool = False) -> int:
        """Speak a fixed line. Returns the mu-law bytes actually sent."""
        spoken = text.strip()
        sent_bytes = 0
        if not spoken or not self._call:
            if hang_up_after:
                await self._hang_up()
            return sent_bytes
        started = asyncio.get_running_loop().time()
        self._speaking_text = spoken
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
                sent_bytes += len(ulaw)
            log.info("Assistant: %s", spoken[:100])
            if self._call:
                await self._call._emit("transcript", "assistant", spoken)
            self._messages.append({"role": "assistant", "content": spoken})
            self._speaking_text = ""
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.error("Fixed TTS error: %s", exc)
        finally:
            if hang_up_after:
                await self._wait_out_playback(sent_bytes, started)
                await self._hang_up()
        return sent_bytes

    async def _wait_out_playback(self, sent_bytes: int, started: float) -> None:
        """Hold until the phone has actually played what we sent."""
        remaining = _remaining_playback_seconds(
            sent_bytes, started, asyncio.get_running_loop().time()
        )
        if remaining <= 0:
            return
        log.info("Waiting %.1fs for the last line to finish playing", remaining)
        try:
            await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            pass

    async def _hang_up(self) -> None:
        hangup = getattr(self._call, "hangup", None)
        if not callable(hangup):
            return
        log.info("Hanging up training call")
        try:
            await hangup()
        except Exception as exc:
            log.warning("hangup failed: %s", exc)

    def _pause_before_this_utterance(self) -> float | None:
        """Silence between the last final and the trainee speaking again.

        None when there is nothing to measure: the first utterance of the
        call, or one Deepgram gave us no usable interim for, in which case
        `_speech_started_at` still points at some earlier utterance.
        """
        if self._last_final_at is None or self._speech_started_at is None:
            return None
        if self._speech_started_at <= self._last_final_at:
            return None
        return self._speech_started_at - self._last_final_at

    def _absorb_unanswered_turn(self, text: str) -> str:
        """Fold an unanswered user message back into this one.

        Taking it off the history keeps one user message per trainee turn
        instead of one per Deepgram final -- which is also the difference
        between the model answering a whole thought and answering its last
        four words. When the trailing message is our own reply, though, the
        merge was decided on the pause alone: that reply has already been
        spoken and stays, and this fragment goes in on its own.
        """
        last = self._messages[-1] if self._messages else None
        if not last or last.get("role") != "user":
            return text
        previous = str(self._messages.pop().get("content") or "").strip()
        return f"{previous} {text}" if previous else text

    def _echo_reference(self) -> str:
        """Text to match a transcript against when deciding if it is our echo."""
        return self._speaking_text or self._last_assistant_text()

    def _last_assistant_text(self) -> str:
        for message in reversed(self._messages):
            if message.get("role") == "assistant" and message.get("content"):
                return str(message["content"])
        return ""
