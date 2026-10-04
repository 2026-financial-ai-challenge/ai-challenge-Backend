"""Scenario ids are stored identifiers, not labels.

calls.scenario_id rows, CALL_SCENARIO and the managed agent names
(spc-<id>-<variant>) all hold these strings. Renaming one makes get_scenario()
quietly fall back to the default playbook, so past calls get re-scored against
the wrong scam. If this test fails, add the old id to _ALIASES instead.
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
    # resolve_agent_id looks agents up by this name; a new format orphans every synced agent
    assert ma.agent_name("card_delivery", "external_tts") == (
        "spc-card_delivery-external_tts"
    )
