"""시나리오 id는 통화 기록, CALL_SCENARIO, 에이전트 이름(spc-<id>-<방식>)에 저장된다.

id를 바꾸면 get_scenario()가 말없이 기본 시나리오로 가서 지난 통화가 다른 시나리오로 채점된다.
이 테스트가 실패하면 예전 id를 _ALIASES에 넣는다.
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
    assert get_scenario("voice_phishing_training").opening_line == (
        SCENARIOS["bank_security_hold"].opening_line
    )


def test_agent_name_format_is_pinned():
    # resolve_agent_id가 이 이름으로 찾는다. 형식이 바뀌면 만들어 둔 에이전트를 모두 못 찾는다
    assert ma.agent_name("card_delivery", "external_tts") == (
        "spc-card_delivery-external_tts"
    )
