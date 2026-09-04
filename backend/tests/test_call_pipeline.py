from types import SimpleNamespace

import pytest

from app.services.call_service import _supported_kwargs, phone_system_prompt
from app.training.deepgram_stt import UtteranceAssembler
from app.training.pipeline_session import (
    MAX_TURN_FRAGMENTS,
    TURN_PAUSE_SECONDS,
    _remaining_playback_seconds,
    continues_previous_turn,
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
    ideal = scenario.ideal_trainee_response or ""
    assert "112" in ideal and "1332" in ideal
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
    monkeypatch.setenv("ELEVENLABS_VOICE_RANDOM", "false")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "testvoice123")
    monkeypatch.delenv("ELEVENLABS_STABILITY", raising=False)
    monkeypatch.delenv("CALL_LLM_PROVIDER", raising=False)
    from app.services import call_service
    from app.training.pipeline_session import PhonePipelineSession

    scenario = get_call_scenario()
    session = call_service.build_pipeline_session(scenario)
    assert isinstance(session, PhonePipelineSession)
    assert session._stt._language == "ko"
    assert session._llm.model == "gpt-4o-mini"
    assert session._tts.voice_id == "testvoice123"
    assert session._tts._stability == scenario.tts_stability
    assert session._llm._max_tokens == 120
    assert session._opening_line == scenario.opening_line
    assert session._max_turns == scenario.max_turns
    assert scenario.opening_line in session._system_prompt
    assert session._greeting is True


def test_build_pipeline_session_uses_gemini_when_selected(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_RANDOM", "false")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "testvoice123")
    monkeypatch.delenv("ELEVENLABS_STABILITY", raising=False)
    monkeypatch.setenv("CALL_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    from app.services import call_service

    scenario = get_call_scenario()
    session = call_service.build_pipeline_session(scenario)
    assert session._llm.provider == "gemini"
    assert session._llm.model == "gemini-3.5-flash-lite"
    # Gemini gets a higher default than OpenAI (call_service._call_max_tokens):
    # it counts hidden thinking tokens against the same budget, so a tight
    # cap can be spent entirely on reasoning and return empty content.
    assert session._llm._max_tokens == 512


def test_call_llm_falls_back_to_the_other_provider_when_both_keys_present(monkeypatch):
    """Primary provider's key can run out mid-deployment; the call should
    still be answerable on whichever other key is configured."""
    monkeypatch.setenv("CALL_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    from app.services.call_service import _build_call_llm
    from app.training.fallback_llm import FallbackLLM
    from clawops.agent.pipeline import GeminiLLM, OpenAILLM

    llm = _build_call_llm(GeminiLLM, OpenAILLM)
    assert isinstance(llm, FallbackLLM)
    assert llm.provider == "gemini"  # primary is still what CALL_LLM_PROVIDER says


def test_call_llm_skips_fallback_when_secondary_key_is_absent(monkeypatch):
    monkeypatch.setenv("CALL_LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from app.services.call_service import _build_call_llm
    from app.training.fallback_llm import FallbackLLM
    from clawops.agent.pipeline import GeminiLLM, OpenAILLM

    llm = _build_call_llm(GeminiLLM, OpenAILLM)
    assert not isinstance(llm, FallbackLLM)
    assert llm.provider == "gemini"


def test_env_voice_id_overrides_scenario(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_RANDOM", "false")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "clonedvoiceid123")
    from app.services.call_service import _tts_voice_id

    scenario = get_call_scenario()
    assert _tts_voice_id(scenario) == "clonedvoiceid123"


def test_random_voice_mode_uses_available_voice(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_RANDOM", "true")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "ignoredvoiceid")
    monkeypatch.setattr("ai.voices.random_voice_id", lambda: "randomvoiceid123")
    from app.services.call_service import _tts_voice_id

    scenario = get_call_scenario()
    assert _tts_voice_id(scenario) == "randomvoiceid123"


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


def test_greeting_guard_is_short_enough_to_stay_interruptible():
    """A real caller gets cut off. The guard only protects the identifying
    first breath so the scenario setup lands; after that the trainee can
    interrupt like on any phone call."""
    from app.training.pipeline_session import GREETING_GUARD_SECONDS

    assert 1.0 <= GREETING_GUARD_SECONDS <= 5.0


def test_remaining_playback_accounts_for_the_engine_buffer():
    """send_audio() returns as soon as the engine takes the bytes, but the
    phone plays them at 8 kHz for seconds after. Ending the turn (hang up, or
    releasing the barge-in lock) without waiting clips the last line."""
    # 16000 bytes of mu-law == 2.0s of audio; 0.5s has elapsed since we started
    # sending, so 1.5s is still unplayed.
    assert _remaining_playback_seconds(16000, started=0.0, now=0.5) == 1.5
    # Already fully played.
    assert _remaining_playback_seconds(16000, started=0.0, now=9.0) == 0.0
    # Nothing was sent (TTS failed) -- never wait.
    assert _remaining_playback_seconds(0, started=0.0, now=0.0) == 0.0
    # A bogus byte count must not park the call forever.
    assert _remaining_playback_seconds(10**9, started=0.0, now=0.0) == 30.0




def test_garbage_transcript_filters_dtmf_noise():
    assert is_garbage_transcript("4 4 4 4 4 4 4 4 4 4 4 4 4 4 4 4")
    assert not is_garbage_transcript("지금은 끊겠습니다")


def test_wants_hang_up_detects_closing():
    assert wants_hang_up("지금은 끊겠습니다")
    assert wants_hang_up("전화 끊을게요")
    assert not wants_hang_up("그건 제 개인정보인데요")


def test_wants_hang_up_covers_natural_korean_closings():
    """A real trainee said "끝낼게" and the old pattern missed it, so the
    caller never ran its hold-then-hang-up step and the report withheld the
    +15 "전화 종료(빠른 판단)" credit."""
    for closing in (
        "미안한데 그 정도 돈은 없어가지고 끝낼게",
        "그만할게요",
        "더 이상 통화 안 하겠습니다",
        "통화 그만하죠",
        "실례합니다 이만 끊습니다",
        "수고하세요",
    ):
        assert wants_hang_up(closing), closing


def test_wants_hang_up_ignores_refusals_and_amounts():
    """Refusing the request is not ending the call -- the scenarios keep
    pushing through it. And "이만" is also the number 20,000, which
    delivery_payment_error says out loud."""
    for keep_going in (
        "이만 삼천 원이 더 빠져나갔다고요?",
        "보증료가 사십오만 원이라고요",
        "안 할래요",
        "그건 제 개인정보인데요",
        "네 알겠습니다",
    ):
        assert not wants_hang_up(keep_going), keep_going


def test_hang_up_ignores_a_closing_quoted_mid_answer():
    """A long answer is one utterance now that a split turn is merged back
    together, and inside one the same words are usually reported speech --
    the trainee talks straight past them instead of hanging up."""
    assert not wants_hang_up(
        "그 사람이 전화 끊으라고 하던데 저는 무슨 소린지 몰라서 계속 듣고만 있었어요"
    )
    # Same length of answer, but this one really does end there.
    assert wants_hang_up("은행에 직접 가서 확인해 보겠습니다 그러니까 이만 끊겠습니다")


def test_pipeline_and_report_share_one_hang_up_rule():
    """Widening one copy and not the other would change the score without
    changing the conversation -- and that now covers where in an utterance
    the closing has to land, not only which words count."""
    from app.services.report_service import _trainee_tried_hangup
    from ai.hangup import wants_hang_up as pipeline_rule

    for utterance in (
        "됐고 이만 끊겠습니다",
        "그 사람이 전화 끊으라고 하던데 저는 무슨 소린지 몰라서 계속 듣고만 있었어요",
        "그건 제 개인정보인데요",
    ):
        assert _trainee_tried_hangup(utterance) is pipeline_rule(utterance), utterance


def test_split_long_answer_is_one_turn_not_four():
    """`endpointing` closes a final after 400ms of silence, so one long answer
    arrives as several. Counting each as a turn spent the whole `max_turns`
    budget on a single answer and hung up on the trainee mid-sentence."""
    history = [
        {"role": "assistant", "content": "본인 확인이 필요합니다."},
        {"role": "user", "content": "아니 그러니까 제가"},
    ]
    assert continues_previous_turn(history, fragments=1, pause_seconds=0.3)


def test_answered_turn_after_a_real_pause_is_a_new_turn():
    """We committed a reply and they waited before answering it -- the one
    case that has to keep costing a turn, or the call never ends."""
    history = [
        {"role": "user", "content": "누구세요"},
        {"role": "assistant", "content": "중앙지검 수사관입니다."},
    ]
    assert not continues_previous_turn(history, fragments=0, pause_seconds=3.0)


def test_resuming_immediately_is_the_same_breath_even_after_a_reply():
    """Nothing we say reaches them and comes back answered inside a second,
    so a fast resume is them finishing their thought over our reply --
    whatever the history looks like."""
    history = [
        {"role": "user", "content": "잠깐만요"},
        {"role": "assistant", "content": "시간이 없습니다."},
    ]
    assert continues_previous_turn(
        history, fragments=0, pause_seconds=TURN_PAUSE_SECONDS
    )
    assert not continues_previous_turn(
        history, fragments=0, pause_seconds=TURN_PAUSE_SECONDS + 0.1
    )


def test_unanswered_turn_merges_however_long_the_llm_took():
    """A slow LLM turn is still one trainee answer: they paused for four
    seconds, we said nothing, they carried on."""
    history = [{"role": "user", "content": "그 계좌가"}]
    assert continues_previous_turn(history, fragments=0, pause_seconds=4.0)


def test_merging_stops_before_it_can_strand_the_call():
    """A pipeline that has stopped answering would otherwise merge forever and
    never let `user_turns` reach `max_turns`."""
    history = [{"role": "user", "content": "어"}]
    assert not continues_previous_turn(
        history, fragments=MAX_TURN_FRAGMENTS, pause_seconds=0.1
    )


def test_continuation_falls_back_to_history_without_a_pause_reading():
    """Deepgram does not always give a usable interim, and the first utterance
    of a call has no previous final to measure from."""
    assert continues_previous_turn(
        [{"role": "user", "content": "제가 말씀드리는 건"}],
        fragments=0,
        pause_seconds=None,
    )
    assert not continues_previous_turn(
        [{"role": "assistant", "content": "안녕하세요."}],
        fragments=0,
        pause_seconds=None,
    )
    assert not continues_previous_turn([], fragments=0, pause_seconds=None)


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


def test_call_had_transcript_accepts_assistant_only_call(monkeypatch):
    from app.services import call_service

    monkeypatch.setattr(
        call_service,
        "get_report",
        lambda _id: SimpleNamespace(
            turns=[SimpleNamespace(role="assistant", text="안녕하세요")]
        ),
    )
    assert call_service.call_had_transcript("ses_1")

    monkeypatch.setattr(
        call_service,
        "get_report",
        lambda _id: SimpleNamespace(turns=[]),
    )
    assert not call_service.call_had_transcript("ses_1")
