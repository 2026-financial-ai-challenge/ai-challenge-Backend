from types import SimpleNamespace

import pytest

from app.services.call_service import _supported_kwargs, phone_system_prompt
from app.training.deepgram_stt import UtteranceAssembler
from app.training.pipeline_session import (
    greeting_playback_seconds,
    is_echo,
    is_garbage_transcript,
    should_hang_up_now,
    wants_hang_up,
)
from app.training.scenarios import ensure_ai_importable, get_call_scenario


def test_default_call_scenario_uses_training_fallback(monkeypatch):
    monkeypatch.delenv("CALL_SCENARIO", raising=False)
    scenario = get_call_scenario()
    assert scenario.id == "voice_phishing_training"
    assert scenario.max_turns == 8
    assert scenario.tts_voice_id
    assert "권위 사칭" in scenario.tactics
    assert "112/1332" in (scenario.ideal_trainee_response or "")
    assert "동적 시나리오 생성을 위한 기본 시드" not in scenario.system_prompt


def test_call_scenario_env_override(monkeypatch):
    monkeypatch.setenv("CALL_SCENARIO", "custom_training_type")
    scenario = get_call_scenario()
    assert scenario.id == "custom_training_type"


def test_phone_prompt_includes_opening_and_forbids_transfer():
    scenario = SimpleNamespace(
        system_prompt="역할 프롬프트",
        opening_line="안녕하세요. 확인 전화입니다.",
        max_turns=6,
    )
    prompt = phone_system_prompt(scenario)
    assert "안녕하세요. 확인 전화입니다." in prompt
    assert "인사말을 다시 하지 않는다" in prompt
    assert "안 들린다" in prompt
    assert "최대 6번" in prompt
    assert "다른 번호로 전화를 돌리지 않는다" in prompt
    assert "문장 둘" in prompt


def test_supported_kwargs_drops_unknown_params():
    class Fake:
        def __init__(self, voice_id: str, model: str = "x"):
            self.voice_id = voice_id
            self.model = model

    kwargs = _supported_kwargs(
        Fake,
        voice_id="abc",
        model="eleven_turbo_v2_5",
        greeting="안녕하세요",
    )
    assert kwargs == {"voice_id": "abc", "model": "eleven_turbo_v2_5"}


def test_call_service_runtime_dependencies_are_imported():
    from app.services import call_service

    assert callable(call_service.register_transcript_listener)
    assert callable(call_service.bind_call)
    assert call_service.SessionLocal is not None
    assert call_service.Call is not None


def test_build_pipeline_session_uses_scenario_voice(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "testvoice123")
    monkeypatch.delenv("ELEVENLABS_STABILITY", raising=False)
    from app.services import call_service
    from app.training.pipeline_session import PhonePipelineSession

    scenario = get_call_scenario()
    session = call_service.build_pipeline_session(scenario)
    assert isinstance(session, PhonePipelineSession)
    assert session._stt._language == "ko"
    assert session._llm.model == "gpt-4o-mini"
    assert session._tts.voice_id == "testvoice123"
    assert session._tts._stability == scenario.tts_stability
    assert session._llm._max_tokens == 180
    assert session._opening_line == scenario.opening_line
    assert session._max_turns == scenario.max_turns
    assert scenario.opening_line in session._system_prompt
    assert session._greeting is True


def test_env_voice_id_overrides_scenario(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "clonedvoiceid123")
    from app.services.call_service import _tts_voice_id

    scenario = get_call_scenario()
    assert _tts_voice_id(scenario) == "clonedvoiceid123"


def _results(transcript: str, *, is_final: bool, speech_final: bool) -> dict:
    return {
        "type": "Results",
        "is_final": is_final,
        "speech_final": speech_final,
        "channel": {"alternatives": [{"transcript": transcript}]},
    }


def test_utterance_assembler_flushes_split_speech_final():
    assembler = UtteranceAssembler()
    assert assembler.ingest(_results("뭘 더하시나요", is_final=False, speech_final=False)) == [
        ("interim", "뭘 더하시나요")
    ]
    assert assembler.ingest(_results("뭘 더하시나요", is_final=True, speech_final=False)) == []
    assert assembler.ingest(_results("", is_final=True, speech_final=True)) == [
        ("final", "뭘 더하시나요")
    ]


def test_utterance_assembler_flushes_on_utterance_end():
    assembler = UtteranceAssembler()
    assembler.ingest(_results("계좌요", is_final=True, speech_final=False))
    assert assembler.ingest({"type": "UtteranceEnd"}) == [("final", "계좌요")]


def test_utterance_assembler_ignores_empty_vad():
    assembler = UtteranceAssembler()
    assert assembler.ingest({"type": "SpeechStarted"}) == []


def test_echo_matches_assistant_playback():
    assistant = "안녕하세요. 중앙금융보안센터 고객보호팀 김정훈입니다."
    assert is_echo("안녕하세요", assistant)
    assert is_echo(
        "지금은 상황을 넘길 상황이 아닙니다",
        "지금은 장난처럼 넘길 상황이 아닙니다. 성함을 말씀해 주십시오.",
    )
    assert not is_echo("왜 안들려", assistant)
    assert not is_echo("대표번호로 다시 확인하겠습니다", assistant)


def test_greeting_playback_covers_opening_line():
    text = (
        "안녕하세요. 중앙금융보안센터 고객보호팀 김정훈입니다. "
        "고객님 계좌에서 이상 거래가 확인되어 연락드렸습니다."
    )
    seconds = greeting_playback_seconds(text)
    assert 6.0 <= seconds <= 12.0
    assert greeting_playback_seconds("") == 8.0


def test_garbage_transcript_filters_dtmf_noise():
    assert is_garbage_transcript("4 4 4 4 4 4 4 4 4 4 4 4 4 4 4 4")
    assert not is_garbage_transcript("지금은 끊겠습니다")


def test_wants_hang_up_detects_closing():
    assert wants_hang_up("지금은 끊겠습니다")
    assert wants_hang_up("전화 끊을게요")
    assert not wants_hang_up("그건 제 개인정보인데요")


def test_first_hangup_keeps_call_second_ends_it():
    assert not should_hang_up_now(hangup_attempts=1, user_turns=3, max_turns=12)
    assert should_hang_up_now(hangup_attempts=2, user_turns=4, max_turns=12)
    assert should_hang_up_now(hangup_attempts=0, user_turns=12, max_turns=12)


def test_trainee_spoke_requires_user_utterance(monkeypatch):
    from app.services import call_service

    monkeypatch.setattr(
        call_service,
        "get_report",
        lambda _id: SimpleNamespace(
            turns=[SimpleNamespace(role="assistant", text="안녕하세요")]
        ),
    )
    assert not call_service.trainee_spoke("ses_1")

    monkeypatch.setattr(
        call_service,
        "get_report",
        lambda _id: SimpleNamespace(
            turns=[SimpleNamespace(role="user", text=" 누구세요  ")]
        ),
    )
    assert call_service.trainee_spoke("ses_1")
