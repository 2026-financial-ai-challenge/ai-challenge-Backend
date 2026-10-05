"""Runtime harness (ai/harness.py): what the caller may say, and when to stop."""

import asyncio

import pytest

from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()

from ai.harness import (  # noqa: E402
    SAFETY_EXIT_LINE,
    CallMonitor,
    GuardedLLM,
    OutputGuard,
    redact_numbers,
)


@pytest.mark.parametrize(
    ("sentence", "violation"),
    [
        ("사실 저는 AI 상담원입니다.", "persona_break"),
        ("이건 보이스피싱 훈련이에요.", "persona_break"),
        ("국민은행 보안팀에서 연락드렸습니다.", "real_org"),
        ("본인 확인을 위해 비밀번호 네 자리를 말씀해 주세요.", "secret_request"),
        ("문자로 받으신 인증번호 불러 주십시오.", "secret_request"),
    ],
)
def test_guard_drops_what_the_prompt_forbids(sentence, violation):
    verdict = OutputGuard().check(sentence)
    assert verdict.dropped
    assert violation in verdict.violations


def test_guard_keeps_the_scammer_line_that_names_a_secret_without_asking():
    """ "저희는 비밀번호는 절대 안 여쭙니다" is the most trust-building thing a
    real crew says. Dropping it would make every caller weaker than a real one."""
    for line in (
        "의심하시는 게 맞습니다. 그래서 저희는 카드 번호나 비밀번호는 절대 안 여쭙습니다.",
        "계좌번호나 비밀번호는 여쭙지 않습니다. 성함 하나만 확인하면 됩니다.",
    ):
        verdict = OutputGuard().check(line)
        assert not verdict.dropped, line
        assert "secret_request" not in verdict.violations


def test_guard_rewrites_reusable_tokens_and_markup():
    verdict = OutputGuard().check("**계좌는** 110-123-456789 로 보내세요 (단호하게).")
    assert "reusable_token" in verdict.violations
    assert "456789" not in verdict.text
    assert "*" not in verdict.text and "단호하게" not in verdict.text


class _Tokens:
    model = "fake-model"
    provider = "fake"
    _max_tokens = 99

    def __init__(self, tokens):
        self.tokens = tokens
        self.seen_messages = None

    async def generate(self, messages, tools=None):
        self.seen_messages = messages
        for token in self.tokens:
            yield token


async def _collect(llm, messages=None):
    out = []
    async for chunk in llm.generate(messages or [{"role": "system", "content": "SYS"}]):
        out.append(chunk)
    return out


def test_guarded_llm_filters_sentence_by_sentence_and_proxies_attributes():
    inner = _Tokens(["네, 확인", "됐습니다. 저는 A", "I라서요. 성함", "만 말씀하십시오."])
    monitor = CallMonitor(scenario_id="t")
    llm = GuardedLLM(inner, monitor=monitor)
    chunks = asyncio.run(_collect(llm))
    spoken = "".join(chunks)
    assert "AI" not in spoken
    assert "확인됐습니다." in spoken and "성함만 말씀하십시오." in spoken
    assert llm.model == "fake-model" and llm.provider == "fake" and llm._max_tokens == 99
    assert monitor.violations == 1


def test_guarded_llm_passes_tool_calls_through_untouched():
    marker = '{"type":"tool_calls","tool_calls":[]}'
    llm = GuardedLLM(_Tokens(["끊겠습니다.", marker]))
    assert asyncio.run(_collect(llm))[-1] == marker


def test_guarded_llm_caps_sentences_per_turn():
    llm = GuardedLLM(_Tokens(["하나. ", "둘. ", "셋. ", "넷. ", "다섯."]))
    assert len(asyncio.run(_collect(llm))) == 3


def test_monitor_corrections_are_merged_into_the_one_system_message():
    """Some clients keep only the last system message (AnthropicLLM does), so
    a second system message would silently drop the whole scenario prompt."""
    inner = _Tokens(["네."])
    monitor = CallMonitor()
    monitor.observe_violation(("secret_request",), "비밀번호 알려 주세요.")
    asyncio.run(_collect(GuardedLLM(inner, monitor=monitor)))
    systems = [m for m in inner.seen_messages if m["role"] == "system"]
    assert len(systems) == 1
    assert systems[0]["content"].startswith("SYS")
    assert "[감독 지시" in systems[0]["content"]
    # consumed once
    assert monitor.take_corrections() == []


def test_monitor_flags_repetition_for_the_next_turn():
    monitor = CallMonitor()
    monitor.observe_assistant("성함만 말씀해 주십시오.")
    monitor.observe_assistant("성함만 말씀해 주십시오!")
    assert any("반복" in note for note in monitor.take_corrections())


def test_monitor_exits_on_real_distress_and_names_the_exercise():
    action = CallMonitor().observe_user("잠깐만요 가슴이 아파서 숨이 안 쉬어져요")
    assert action.kind == "exit"
    assert action.line == SAFETY_EXIT_LINE
    assert "훈련" in action.line


def test_monitor_does_not_exit_on_the_fear_the_scenario_is_built_to_provoke():
    monitor = CallMonitor()
    for line in ("무서워요 어떡해요", "저 진짜 몰라요", "경찰에 신고할게요"):
        assert monitor.observe_user(line).kind == "continue", line


def test_monitor_stops_a_trainee_reading_out_a_number_then_ends_the_call():
    monitor = CallMonitor()
    first = monitor.observe_user("카드번호는 1234 5678 9012 3456 이에요")
    assert first.kind == "redirect"
    second = monitor.observe_user("계좌는 110 234 567890")
    assert second.kind == "exit"


def test_monitor_ends_in_character_once_the_guard_budget_is_spent():
    monitor = CallMonitor(hangup_line="확인 거부로 기록하겠습니다.", max_violations=2)
    monitor.observe_violation(("persona_break",), "x")
    monitor.observe_violation(("real_org",), "y")
    action = monitor.observe_user("네")
    assert action.kind == "exit"
    assert action.line == "확인 거부로 기록하겠습니다."


def test_redact_numbers_keeps_the_words():
    assert redact_numbers("제 번호는 1234 5678 9012 입니다") == "제 번호는 [번호 생략] 입니다"
