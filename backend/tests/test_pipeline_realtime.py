"""Turn-taking behaviour of PhonePipelineSession: latency, barge-in, script mode."""

import asyncio
import logging
import time

import pytest
from clawops.agent.pipeline import PipelineSession, SpeechEvent

from app.training.pipeline_session import (
    RESPONSE_DEBOUNCE_SECONDS,
    PhonePipelineSession,
    PlaybackClock,
    cut_to_heard,
    is_backchannel,
)
from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()

from ai.harness import SAFETY_EXIT_LINE, CallMonitor  # noqa: E402
from ai.scenarios.script import ScriptReply, ScriptRouter  # noqa: E402


class FakeCall:
    def __init__(self):
        self.sent = []
        self.cleared = 0
        self.hung = False
        self.events = []

    async def send_audio(self, audio):
        self.sent.append(audio)

    async def clear_audio(self):
        self.cleared += 1

    async def hangup(self):
        self.hung = True

    async def _emit(self, *args):
        self.events.append(args)


class FakeTTS:
    sample_rate = 8000

    def __init__(self):
        self.calls = 0

    async def synthesize(self, text_stream):
        self.calls += 1
        async for _ in text_stream:
            yield b"\x00\x00" * 800  # 0.1 s of PCM16 silence


class ExplodingLLM:
    model = "x"

    async def generate(self, messages, tools=None):
        raise AssertionError("the LLM must not be called on this path")
        yield  # pragma: no cover


def _session(**kwargs):
    defaults = dict(stt=object(), llm=ExplodingLLM(), tts=FakeTTS(), greeting=False)
    defaults.update(kwargs)
    session = PhonePipelineSession(**defaults)
    call = FakeCall()
    session._call = call
    session._running = True
    session._greeting_playing = False
    session._messages = [{"role": "system", "content": "SYS"}]
    session._track_playback(call)
    return session, call


def test_debounce_is_well_under_clawops_half_second():
    assert RESPONSE_DEBOUNCE_SECONDS <= 0.2

    async def run():
        session, _call = _session()
        started = []

        async def fake_respond():
            started.append(time.monotonic())

        session._respond = fake_respond
        t0 = time.monotonic()
        await session._debounced_respond(0.5)
        await asyncio.sleep(0)
        return started[0] - t0

    assert asyncio.run(run()) < 0.3


@pytest.mark.parametrize("text", ["네", "네네", "음", "예 예", "아 네", "그렇죠", "응."])
def test_backchannel_words(text):
    assert is_backchannel(text)


@pytest.mark.parametrize("text", ["네 근데 누구세요", "잠깐만요", "아니요", "네? 얼마요?"])
def test_real_turns_are_not_backchannel(text):
    assert not is_backchannel(text)


def test_backchannel_over_our_line_does_not_cut_us_off():
    async def run():
        session, call = _session()
        session._sent_audio_chunks = 1
        session._playback.sent(16000)  # 2 s still queued
        await session._handle_interim_speech(SpeechEvent(type="interim", transcript="네네"))
        assert call.cleared == 0
        await session._handle_interim_speech(SpeechEvent(type="interim", transcript="잠깐만요"))
        assert call.cleared == 1

    asyncio.run(run())


def test_interrupted_line_is_remembered_as_far_as_it_was_heard():
    now = [0.0]
    clock = PlaybackClock(now=lambda: now[0])
    clock.begin_utterance()
    clock.sent(16000)  # 2.0 s line
    now[0] = 0.5
    seconds, fraction = clock.heard()
    assert seconds == pytest.approx(0.5) and fraction == pytest.approx(0.25)

    async def run():
        session, _call = _session()
        session._playback = clock
        session._messages.append({"role": "user", "content": "누구세요"})
        session._messages.append({"role": "assistant", "content": "가온금융안전원 결제보호팀 서동현입니다 확인차 연락드렸습니다."})
        session._note_interruption()
        return session._messages[-1]["content"]

    heard = asyncio.run(run())
    assert heard.endswith("…")
    assert len(heard) < len("가온금융안전원 결제보호팀 서동현입니다 확인차 연락드렸습니다.")


def test_cut_to_heard_keeps_nearly_finished_lines_whole():
    assert cut_to_heard("성함만 말씀하십시오.", 0.95) == "성함만 말씀하십시오."


def test_prerendered_line_plays_without_tts():
    async def run():
        tts = FakeTTS()
        session, call = _session(tts=tts, prerendered=lambda text: b"\x55" * 4000)
        await session._speak_fixed("성함만 말씀하십시오.")
        return tts.calls, sum(len(b) for b in call.sent), session._messages[-1]

    tts_calls, sent, last = asyncio.run(run())
    assert tts_calls == 0
    assert sent == 4000
    assert last == {"role": "assistant", "content": "성함만 말씀하십시오."}


def test_harness_exit_names_the_exercise_and_hangs_up():
    async def run():
        session, call = _session(monitor=CallMonitor())
        await session._handle_final_transcript(
            SpeechEvent(type="final", transcript="가슴이 아파서 숨이 안 쉬어져요")
        )
        return session, call

    session, call = asyncio.run(run())
    assert call.hung
    assert session._messages[-1]["content"] == SAFETY_EXIT_LINE
    # the trainee's turn reaches the live transcript even though clawops never saw it
    assert ("transcript", "user", "가슴이 아파서 숨이 안 쉬어져요") in call.events


def test_trainee_number_is_redacted_from_history_and_transcript():
    async def run():
        session, call = _session(monitor=CallMonitor())
        await session._handle_final_transcript(
            SpeechEvent(type="final", transcript="카드번호 1234 5678 9012 3456 불러드릴게요")
        )
        await asyncio.sleep(0.05)
        return session, call

    session, call = asyncio.run(run())
    user_turns = [m["content"] for m in session._messages if m["role"] == "user"]
    assert user_turns and "1234" not in user_turns[-1]
    assert all("5678" not in str(e) for e in call.events)


def _script_router():
    return ScriptRouter(
        progression=("사고 접수 들어가겠습니다.",),
        replies=(ScriptReply("deny", ("그러면 명의 도용 건입니다.",)),),
    )


def test_script_mode_on_answers_from_the_script_without_the_llm():
    async def run():
        session, call = _session(script_router=_script_router(), script_mode="on")
        await session._handle_final_transcript(SpeechEvent(type="final", transcript="아니요"))
        await session._current_response_task
        return session

    session = asyncio.run(run())
    assert session._messages[-1] == {"role": "assistant", "content": "그러면 명의 도용 건입니다."}
    assert session._messages[-2] == {"role": "user", "content": "아니요"}


def test_script_mode_shadow_logs_the_decision_and_lets_the_llm_answer(monkeypatch, caplog):
    handed_over = []

    async def fake_super(self, event):
        handed_over.append(event.transcript)

    monkeypatch.setattr(PipelineSession, "_handle_final_transcript", fake_super)

    async def run():
        session, _call = _session(script_router=_script_router(), script_mode="shadow", scenario_id="demo")
        await session._handle_final_transcript(SpeechEvent(type="final", transcript="아니요"))

    with caplog.at_level(logging.INFO, logger="clawops.agent.pipeline"):
        asyncio.run(run())
    assert handed_over == ["아니요"]
    assert any('SCRIPT {"mode": "shadow", "scenario": "demo"' in r.getMessage() for r in caplog.records)
