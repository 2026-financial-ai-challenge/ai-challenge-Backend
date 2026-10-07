"""고정 훈련 시나리오 조회. 시나리오 내용은 ai/scenarios/library.py에 있다."""

from __future__ import annotations

from dataclasses import replace
from secrets import choice

from ai.scenarios.library import PLAYBOOKS
from ai.scenarios.playbook import Playbook

__all__ = [
    "DEFAULT_SCENARIO_ID",
    "PLAYBOOKS",
    "Playbook",
    "SCENARIOS",
    "canonical_id",
    "get_scenario",
    "pick_scenario",
]

SCENARIOS: dict[str, Playbook] = {playbook.id: playbook for playbook in PLAYBOOKS}

# 모르는 id와 예전 id "voice_phishing_training"이 가는 시나리오
DEFAULT_SCENARIO_ID = "bank_security_hold"

# 예전 id. 저장된 통화 기록과 CALL_SCENARIO 값이 계속 찾을 수 있게 남긴다.
_ALIASES = {
    "voice_phishing_training": DEFAULT_SCENARIO_ID,
    # 시나리오를 바꾸며 바뀐 id
    "delivery_payment_error": "ipo_allocation",
    "family_emergency": "card_delivery",
}

# 예고 전화와 불시 전화가 같은 시나리오로 연달아 나가지 않게 직전 값을 기억한다.
_last_picked_id: str | None = None


def canonical_id(scenario_id: str) -> str:
    """예전 id를 지금 id로 바꾼다. get_scenario()는 요청한 id를 그대로 두므로 id를 비교할 때는 이것을 쓴다."""
    requested = (scenario_id or "").strip()
    return _ALIASES.get(requested, requested)


def get_scenario(scenario_id: str) -> Playbook:
    """id로 시나리오를 찾는다. 모르는 id면 기본 시나리오를 쓰되 id는 요청한 값으로 둔다."""
    requested = (scenario_id or "").strip() or DEFAULT_SCENARIO_ID
    scenario = SCENARIOS.get(canonical_id(requested)) or SCENARIOS[DEFAULT_SCENARIO_ID]
    if scenario.id == requested:
        return scenario
    return replace(scenario, id=requested)


def pick_scenario() -> Playbook:
    """직전과 다른 시나리오를 무작위로 고른다."""
    global _last_picked_id

    pool = [scenario for scenario in SCENARIOS.values() if scenario.id != _last_picked_id]
    picked = choice(pool or list(SCENARIOS.values()))
    _last_picked_id = picked.id
    return picked
