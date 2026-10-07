"""시나리오 id는 DB(calls.scenario_id), CALL_SCENARIO, 에이전트 이름에 저장된다.

id를 바꾸면 기존 통화가 기본 시나리오로 채점된다. 깨지면 이전 id를 _ALIASES에 추가할 것.
"""

from ai import managed_agent as ma
from ai.scenarios import _ALIASES, DEFAULT_SCENARIO_ID, SCENARIOS, get_scenario

PINNED_IDS = {
    "bank_security_hold",
    "low_interest_loan",
    "ipo_allocation",
    "card_delivery",
    "investigation_unit",
}


def test_scenario_ids_are_pinned():
    assert set(SCENARIOS) == PINNED_IDS
    assert DEFAULT_SCENARIO_ID == "bank_security_hold"


def test_each_id_resolves_to_its_own_playbook():
    for scenario_id in PINNED_IDS:
        assert get_scenario(scenario_id) is SCENARIOS[scenario_id]


def test_aliases_are_pinned():
    assert _ALIASES == {
        "voice_phishing_training": "bank_security_hold",
        "delivery_payment_error": "ipo_allocation",
        "family_emergency": "card_delivery",
    }
    assert get_scenario("voice_phishing_training").system_prompt == (
        SCENARIOS["bank_security_hold"].system_prompt
    )


def test_agent_name_format_is_pinned():
    # 이름 형식이 바뀌면 기존 에이전트를 못 찾는다
    assert ma.agent_name("card_delivery", "external_tts") == (
        "spc-card_delivery-external_tts"
    )
