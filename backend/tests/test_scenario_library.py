import asyncio

import pytest

from app.training.scenarios import (
    ensure_ai_importable,
    get_call_scenario,
    get_runtime_scenario,
)


def setup_function() -> None:
    ensure_ai_importable()


EXPECTED_IDS = {
    "bank_security_hold",
    "low_interest_loan",
    "delivery_payment_error",
    "family_emergency",
    "investigation_unit",
}


def test_library_covers_every_expected_scenario():
    from ai.scenarios import SCENARIOS

    assert set(SCENARIOS) == EXPECTED_IDS
    for scenario in SCENARIOS.values():
        assert scenario.opening_line.endswith((".", "?", "!"))
        assert scenario.tactics and scenario.red_flags
        assert scenario.ideal_trainee_response
        assert scenario.hangup_line
        assert 6 <= scenario.max_turns <= 10


def test_library_text_passes_the_generator_safety_gates():
    """Fixed scenarios must clear the same bars a generated one has to clear."""
    from ai.scenarios.generator import _REAL_ORGS, _SPOKEN_META, _UNSAFE_TOKEN
    from ai.scenarios.library import PLAYBOOKS

    for playbook in PLAYBOOKS:
        values = [
            playbook.name,
            playbook.persona_name,
            playbook.organization,
            playbook.role,
            playbook.opening_line,
            playbook.incident,
            playbook.goal,
            playbook.ideal_trainee_response,
            playbook.hangup_line,
            *playbook.turn_plan,
            *playbook.objection_handling,
            *playbook.tactics,
            *playbook.red_flags,
            *(line for pair in playbook.examples for line in pair),
            *(reply for _trigger, reply in playbook.quick_replies),
        ]
        for value in values:
            assert not _SPOKEN_META.search(value), (playbook.id, value)
            assert not _REAL_ORGS.search(value), (playbook.id, value)
            assert not _UNSAFE_TOKEN.search(value), (playbook.id, value)


def test_quick_replies_use_known_triggers():
    from ai.scenarios.library import PLAYBOOKS
    from ai.scenarios.reflex import REFLEX_TRIGGERS

    known = {name for name, _pattern in REFLEX_TRIGGERS}
    for playbook in PLAYBOOKS:
        triggers = [trigger for trigger, _reply in playbook.quick_replies]
        assert set(triggers) <= known, playbook.id
        assert len(triggers) == len(set(triggers)), playbook.id


def test_system_prompt_carries_the_event_and_the_plan():
    from ai.scenarios import get_scenario

    scenario = get_scenario("bank_security_hold")
    assert "[사건" in scenario.system_prompt
    assert "[진행]" in scenario.system_prompt
    assert "[받아치기]" in scenario.system_prompt
    # The examples teach reply length, which is what keeps a turn short.
    assert "[말의 길이와 결은 이 정도로 한다]" in scenario.system_prompt


def test_legacy_scenario_id_still_resolves():
    from ai.scenarios import get_scenario

    scenario = get_scenario("voice_phishing_training")
    assert scenario.id == "voice_phishing_training"
    assert scenario.system_prompt


def test_unknown_scenario_id_keeps_its_name_and_falls_back():
    from ai.scenarios import DEFAULT_SCENARIO_ID, SCENARIOS, get_scenario

    scenario = get_scenario("some_future_training_type")
    assert scenario.id == "some_future_training_type"
    assert scenario.name == SCENARIOS[DEFAULT_SCENARIO_ID].name


def test_pick_scenario_never_repeats_back_to_back():
    from ai.scenarios import pick_scenario

    picks = [pick_scenario().id for _ in range(20)]
    assert set(picks) <= EXPECTED_IDS
    assert all(a != b for a, b in zip(picks, picks[1:]))
    # Over 20 draws a five-scenario pool should not collapse to one or two.
    assert len(set(picks)) >= 3


def test_runtime_scenario_makes_no_llm_call(monkeypatch):
    monkeypatch.setenv("DYNAMIC_SCENARIO", "false")
    monkeypatch.delenv("CALL_SCENARIO", raising=False)

    from ai.scenarios import generator

    def explode(*_args, **_kwargs):
        raise AssertionError("scenario generation must not run on the call path")

    monkeypatch.setattr(generator, "generate_scenario", explode)
    scenario = asyncio.run(get_runtime_scenario())
    assert scenario.id in EXPECTED_IDS


def test_pinned_scenario_disables_rotation(monkeypatch):
    monkeypatch.setenv("DYNAMIC_SCENARIO", "false")
    monkeypatch.setenv("CALL_SCENARIO", "investigation_unit")
    scenario = asyncio.run(get_runtime_scenario())
    assert scenario.id == "investigation_unit"
    assert scenario.id == get_call_scenario().id


@pytest.mark.parametrize(
    ("utterance", "trigger"),
    [
        ("이거 보이스피싱 아니에요?", "scam_accusation"),
        ("잘 안 들리는데요", "not_audible"),
        ("뭐라고요?", "repeat_that"),
        ("누구세요", "who_is_this"),
        ("어디시죠?", "who_is_this"),
        ("지금 좀 바쁜데요", "busy_now"),
        ("제 계좌에서요?", None),
        ("성함은 못 알려드립니다", None),
    ],
)
def test_reflex_trigger_matching(utterance, trigger):
    from ai.scenarios.reflex import match_trigger

    assert match_trigger(utterance) == trigger


def test_reflex_table_fires_once_per_trigger_and_respects_budget():
    from ai.scenarios.reflex import ReflexTable

    table = ReflexTable(
        (
            ("who_is_this", "가온금융안전원 서동현입니다."),
            ("busy_now", "확인 한 가지만 하겠습니다."),
            ("not_audible", "다시 말씀드립니다."),
        ),
        budget=2,
    )
    assert table.take("누구세요") == "가온금융안전원 서동현입니다."
    assert table.take("누구세요") is None  # one shot per trigger
    assert table.take("지금 바빠요") == "확인 한 가지만 하겠습니다."
    assert table.take("안 들려요") is None  # budget spent
    assert table.remaining == 0


def test_reflex_table_is_inert_without_quick_replies():
    from ai.scenarios.reflex import ReflexTable

    assert ReflexTable((), budget=3).take("누구세요") is None
    assert ReflexTable(None, budget=0).take("누구세요") is None
