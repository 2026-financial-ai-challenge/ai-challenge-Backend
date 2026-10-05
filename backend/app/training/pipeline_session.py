"""PipelineSession tweaks for phone greeting lock, short turns, and hang-up.

On top of clawops' PipelineSession this adds what makes the call sound like a
person rather than a turn-based bot:

- no fixed half-second wait before every answer (`RESPONSE_DEBOUNCE_SECONDS`)
- "네", "음" said over the caller do not stop it mid-sentence (`is_backchannel`)
- an interrupted line is remembered as far as it was actually heard
- fixed lines play from pre-synthesized audio with no TTS round trip
- the runtime harness (ai/harness.py) sees every turn in both directions
- the script call mode (ai/scenarios/script.py): `off`, `shadow` (decide and
  log what the script would have said, let the LLM answer), or `on`
"""

from __future__ import annotations

import asyncio
from difflib import SequenceMatcher
import json
import logging
import os
import re
import time
from collections.abc import AsyncIterator, Callable

from clawops.agent._audio import pcm16_to_ulaw, resample_pcm16
from clawops.agent.pipeline import PipelineSession, SpeechEvent

from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()
from ai.hangup import wants_hang_up  # noqa: E402
from ai.harness import CallMonitor, redact_numbers  # noqa: E402
from ai.scenarios.reflex import ReflexTable  # noqa: E402
from ai.scenarios.script import ScriptRouter  # noqa: E402


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


# clawops waits 0.5 s after every final before it even starts the LLM, so a
# second fragment of the same thought can be folded in. Deepgram has already
# waited `endpointing` ms of silence to send that final, and continues_previous_turn
# merges a late fragment on its own, so the full half second was paid on every
# single answer for a case the session already handles.
RESPONSE_DEBOUNCE_SECONDS = float(os.getenv("CALL_RESPONSE_DEBOUNCE_SEC", "0.1"))

# Listener noises said over the caller. On a real call they mean "go on", and
# a caller who stops dead at every "네" is the most machine-like thing there is.
_BACKCHANNEL = re.compile(
    r"^(?:네|예|응|음|어|아|오|흠|으응|그래요?|그렇죠|맞아요|네네|예예|아하|아\s*네|네\s*네)"
    r"(?:[\s.,?!~…]*(?:네|예|응|음|어|아))*[\s.,?!~…]*$"
)
# How much of an interrupted line has to have played before it counts as said.
# Under this the trainee cut in on the first syllable and heard nothing.
_HEARD_FLOOR_SECONDS = 0.35
# Prerendered audio goes out in slices this big (bytes of 8 kHz mu-law ==
# 1/8000 s each), so a cancelled line stops being queued within 0.2 s.
_PRERENDER_SLICE = 1600


def is_backchannel(text: str) -> bool:
    return bool(_BACKCHANNEL.match((text or "").strip()))


class PlaybackClock:
    """Where the phone is in the audio we have queued.

    send_audio() returns as soon as ClawOps accepts the bytes, but the line
    plays them at 8 kHz, so the queue runs seconds ahead of what the trainee
    has heard. This models that queue: every send extends `playing_until` by
    the bytes' duration, a clear_audio() empties it.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self.playing_until = 0.0
        self.utterance_started: float | None = None
        self.total_bytes = 0

    def begin_utterance(self) -> None:
        self.utterance_started = None

    def sent(self, nbytes: int) -> None:
        now = self._now()
        start = max(now, self.playing_until)
        if self.utterance_started is None:
            self.utterance_started = start
        self.playing_until = start + nbytes / _ULAW_BYTES_PER_SECOND
        self.total_bytes += nbytes

    def cleared(self) -> None:
        self.playing_until = min(self.playing_until, self._now())

    def is_playing(self, margin: float = 0.1) -> bool:
        return self._now() < self.playing_until - margin

    def heard(self) -> tuple[float, float]:
        """(seconds heard, fraction heard) of the current utterance."""
        if self.utterance_started is None:
            return 0.0, 0.0
        total = self.playing_until - self.utterance_started
        heard = min(self._now(), self.playing_until) - self.utterance_started
        heard = max(0.0, heard)
        if total <= 0:
            return heard, 1.0
        return heard, min(1.0, heard / total)


def cut_to_heard(text: str, fraction: float) -> str:
    """The part of `text` the trainee heard before cutting in."""
    spoken = (text or "").strip()
    if fraction >= 0.9 or not spoken:
        return spoken
    cut = max(1, int(len(spoken) * fraction))
    head = spoken[:cut]
    space = head.rfind(" ")
    if space >= len(head) // 2:
        head = head[:space]
    return head.rstrip(" ,.") + "…"


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
        monitor: CallMonitor | None = None,
        script_router: ScriptRouter | None = None,
        script_mode: str = "off",
        prerendered: Callable[[str], bytes | None] | None = None,
        scenario_id: str = "",
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
        self._monitor = monitor
        self._router = script_router if script_router and script_router.has_script else None
        mode = (script_mode or "off").strip().lower()
        self._script_mode = mode if mode in {"off", "shadow", "on"} else "off"
        self._prerendered = prerendered
        self._scenario_id = scenario_id
        self._playback = PlaybackClock()
        # Set by a barge-in on the turn being cut off: (seconds, fraction) heard.
        self._interrupted_heard: tuple[float, float] | None = None

    async def prewarm(self) -> None:
        await super().prewarm()
        # The phone is still ringing; open the LLM connection now so the first
        # answer does not pay for the handshake.
        warm = getattr(self._llm, "warm", None)
        if callable(warm):
            self._tasks.append(asyncio.create_task(self._warm(warm)))

    @staticmethod
    async def _warm(warm) -> None:
        try:
            await warm()
        except Exception as exc:  # noqa: BLE001 -- best effort
            log.info("LLM warm-up failed: %s", exc)

    async def attach(self, call) -> None:
        self._track_playback(call)
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
        if self._playback.is_playing() and is_backchannel(text):
            log.info("Backchannel over our line, keep talking: %s", text[:20])
            return
        # Taken here rather than on the final: the gap between two finals also
        # contains however long the trainee spoke, so only the moment their
        # voice comes back measures the pause.
        self._speech_started_at = asyncio.get_running_loop().time()
        self._note_interruption()
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
        if self._playback.is_playing() and is_backchannel(text):
            # Heard it, carry on: not a turn, not an interruption.
            log.info("Backchannel final ignored while speaking: %s", text[:20])
            return
        self._note_interruption()

        log.info("STT: %s", text)
        action = self._monitor.observe_user(text) if self._monitor else None
        if action is not None and action.kind != "continue":
            # The harness outranks the scenario: no turn counting, no merge.
            log.warning("Harness %s (%s)", action.kind, action.reason)
            await self._cancel_inflight()
            await self._commit_user(redact_numbers(text))
            if action.kind == "exit":
                await self._speak_fixed(action.line, hang_up_after=True)
            else:
                self._speak_in_background(action.line)
            return
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
            await self._cancel_inflight()
            await self._commit_user(text)
            await self._speak_fixed(self._hangup_line, hang_up_after=True)
            return

        # Script mode: answer from the scenario's pre-written lines when the
        # router is sure what kind of turn this is. `shadow` only records the
        # decision -- that log is what `python -m ai.script_eval` measures.
        if self._router is not None and self._script_mode != "off":
            decision = (
                self._router.route(text) if self._script_mode == "on" else self._router.peek(text)
            )
            log.info(
                "SCRIPT %s",
                json.dumps(
                    {"mode": self._script_mode, "scenario": self._scenario_id, **decision.as_log()},
                    ensure_ascii=False,
                ),
            )
            if self._script_mode == "on" and decision.hit:
                await self._cancel_inflight()
                await self._commit_user(text)
                self._speak_in_background(decision.line)
                return

        # Fast path: a handful of trainee lines ("안 들려요", "누구세요?") have
        # an answer the scenario already fixes, so answering from the table
        # skips the whole LLM round trip before the first TTS byte. Budgeted
        # and one-shot per trigger, so the call stays LLM-driven. In script
        # mode `on` the router already covers these (quick replies are part
        # of its pool), so the table only runs when the router did not.
        reflex = None if self._script_mode == "on" and self._router else self._reflexes.take(text)
        if reflex:
            log.info("Reflex reply (no LLM): %s", reflex[:40])
            await self._cancel_inflight()
            await self._commit_user(text)
            self._speak_in_background(reflex)
            return

        await super()._handle_final_transcript(event)

    async def _debounced_respond(self, delay: float) -> None:
        await super()._debounced_respond(min(delay, RESPONSE_DEBOUNCE_SECONDS))

    # ── helpers for the paths that answer without the LLM ────────────────

    async def _commit_user(self, text: str) -> None:
        """Record a trainee turn we answer ourselves.

        clawops emits the `transcript` event only on its own LLM path, so a
        turn answered from the reflex table, the script or the harness used to
        be missing from the live transcript the report is built from.
        """
        self._messages.append({"role": "user", "content": text})
        if self._call is not None:
            try:
                await self._call._emit("transcript", "user", text)
            except Exception as exc:  # noqa: BLE001
                log.debug("transcript emit failed: %s", exc)

    async def _cancel_inflight(self) -> None:
        """Stop whatever answer is being prepared or played for an older turn."""
        for attr in ("_pending_respond_task", "_current_response_task"):
            task = getattr(self, attr, None)
            if task is not None and not task.done() and task is not asyncio.current_task():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._playback.is_playing() and self._call is not None:
            await self._call.clear_audio()
        self._sent_audio_chunks = 0

    def _speak_in_background(self, line: str) -> None:
        """Speak a fixed line as the current response, so barge-in can cut it.

        Awaiting it inline would park the STT loop until the line was queued,
        and a trainee talking over it would not be heard until then.
        """
        self._current_response_task = asyncio.create_task(self._speak_fixed(line))

    def _note_interruption(self) -> None:
        """The trainee is talking over us: remember how much they heard."""
        if not self._playback.is_playing():
            return
        heard = self._playback.heard()
        self._interrupted_heard = heard
        seconds, fraction = heard
        last = self._messages[-1] if self._messages else None
        # A line whose audio was fully queued is already in the history as if
        # it had all been heard. Cut it back to what actually played.
        if last and last.get("role") == "assistant" and fraction < 0.9:
            if seconds < _HEARD_FLOOR_SECONDS:
                self._messages.pop()
            else:
                last["content"] = cut_to_heard(str(last.get("content") or ""), fraction)
            log.info("Barge-in after %.1fs (%.0f%% heard)", seconds, fraction * 100)
            self._interrupted_heard = None  # already applied

    def _record_partial(self, spoken: str, turn_marker: int) -> None:
        """Put the heard part of a cancelled line into the history, in order."""
        heard = self._interrupted_heard
        self._interrupted_heard = None
        if heard is None or not spoken.strip():
            return
        seconds, fraction = heard
        if seconds < _HEARD_FLOOR_SECONDS:
            return
        partial = {"role": "assistant", "content": cut_to_heard(spoken, fraction)}
        # The interrupting user turn may already be in the history (a final
        # that arrived without an interim first); ours goes before it.
        index = len(self._messages)
        while index > turn_marker and self._messages[index - 1].get("role") == "user":
            index -= 1
        self._messages.insert(index, partial)

    def _track_playback(self, call) -> None:
        if call is None or getattr(call, "_playback_tracked", False):
            return
        send = getattr(call, "send_audio", None)
        clear = getattr(call, "clear_audio", None)
        if not callable(send):
            return
        clock = self._playback

        async def tracked_send(audio: bytes) -> None:
            clock.sent(len(audio))
            await send(audio)

        async def tracked_clear() -> None:
            clock.cleared()
            if callable(clear):
                await clear()

        try:
            call.send_audio = tracked_send
            call.clear_audio = tracked_clear
            call._playback_tracked = True
        except AttributeError:
            log.info("CallSession does not allow wrapping; playback clock disabled")

    async def _respond(self) -> None:
        """Speak something even when the LLM turn falls over.

        clawops' _respond swallows every exception and returns without
        sending audio, which on a phone call is dead air rather than a
        visible error. When the turn produced no audio and added no message,
        say the stall line so the caller stays in character.
        """
        turn_marker = len(self._messages)
        self._playback.begin_utterance()
        self._interrupted_heard = None
        await super()._respond()

        if not (self._running and self._call):
            return
        task = asyncio.current_task()
        if task is not None and getattr(task, "cancelling", lambda: 0)():
            # Barge-in cancelled this turn; the next one will answer. clawops
            # drops a cancelled answer from the history entirely, so the model
            # would not know it had started saying anything.
            sentences = getattr(self._llm, "turn_sentences", None) or []
            self._record_partial(" ".join(sentences), turn_marker)
            return
        if self._sent_audio_chunks > 0:
            return  # the model did answer
        if len(self._messages) != turn_marker:
            return  # a tool call (e.g. hang_up) or a newer turn moved on

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
        turn_marker = len(self._messages)
        self._speaking_text = spoken
        self._playback.begin_utterance()
        self._interrupted_heard = None
        try:
            self._sent_audio_chunks = 0
            prerendered = self._prerendered(spoken) if self._prerendered else None
            if prerendered:
                # Already phone-format audio: a byte copy, no TTS round trip.
                log.info("Prerendered line (%.1fs): %s", len(prerendered) / 8000, spoken[:40])
                for offset in range(0, len(prerendered), _PRERENDER_SLICE):
                    if not self._running or not self._call:
                        break
                    chunk = prerendered[offset : offset + _PRERENDER_SLICE]
                    self._log_first_audio()
                    await self._call.send_audio(chunk)
                    self._sent_audio_chunks += 1
                    sent_bytes += len(chunk)
            else:

                async def sentences() -> AsyncIterator[str]:
                    yield spoken

                tts_sample_rate = getattr(self._tts, "sample_rate", 24000)
                async for audio in self._tts.synthesize(sentences()):
                    if not self._running or not self._call:
                        break
                    pcm8k = resample_pcm16(audio, from_rate=tts_sample_rate, to_rate=8000)
                    ulaw = pcm16_to_ulaw(pcm8k)
                    self._log_first_audio()
                    await self._call.send_audio(ulaw)
                    self._sent_audio_chunks += 1
                    sent_bytes += len(ulaw)
            log.info("Assistant: %s", spoken[:100])
            if self._call:
                await self._call._emit("transcript", "assistant", spoken)
            self._messages.append({"role": "assistant", "content": spoken})
            if self._monitor is not None:
                self._monitor.observe_assistant(spoken)
            self._speaking_text = ""
        except asyncio.CancelledError:
            self._record_partial(spoken, turn_marker)
        except Exception as exc:
            log.error("Fixed TTS error: %s", exc)
        finally:
            if hang_up_after:
                await self._wait_out_playback(sent_bytes, started)
                await self._hang_up()
        return sent_bytes

    def _log_first_audio(self) -> None:
        if self._first_audio_logged or self._call is None:
            return
        from clawops.agent.pipeline._buffering_call import log_first_realtime_audio

        log_first_realtime_audio(self._call)
        self._first_audio_logged = True

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
