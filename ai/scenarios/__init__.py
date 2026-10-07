"""시나리오 조회·선택. 내용은 library.py."""

from __future__ import annotations

from dataclasses import replace
from secrets import choice

from ai.scenarios.library import DEFAULT_PLAYBOOK_ID, PLAYBOOKS
from ai.scenarios.playbook import Playbook, to_scenario
from ai.scenarios.types import Scenario

__all__ = [
    "DEFAULT_SCENARIO_ID",
    "PLAYBOOKS",
    "Playbook",
    "SCENARIOS",
    "Scenario",
    "canonical_id",
    "get_scenario",
    "pick_scenario",
]

DEFAULT_SCENARIO_ID = DEFAULT_PLAYBOOK_ID

SCENARIOS: dict[str, Scenario] = {
    playbook.id: to_scenario(playbook) for playbook in PLAYBOOKS
}

# 이전 id → 현재 id. 백엔드 기본값과 저장된 통화 기록 때문에 유지.
_ALIASES = {
    "voice_phishing_training": DEFAULT_SCENARIO_ID,
    "delivery_payment_error": "ipo_allocation",
    "family_emergency": "card_delivery",
}

# 연속 중복 방지용. 프로세스 단위라 워커가 여럿이면 각자 따로 센다.
_last_picked_id: str | None = None


def canonical_id(scenario_id: str) -> str:
    """별칭을 현재 id로 바꾼다. get_scenario()는 요청 id를 그대로 두므로 id 비교는 이걸 쓴다."""
    requested = (scenario_id or "").strip()
    return _ALIASES.get(requested, requested)


def get_scenario(scenario_id: str) -> Scenario:
    """모르는 id는 기본 시나리오로 대체하되 id 값은 요청한 그대로 둔다."""
    requested = (scenario_id or "").strip() or DEFAULT_SCENARIO_ID
    key = canonical_id(requested)
    scenario = SCENARIOS.get(key)
    if scenario is None:
        scenario = SCENARIOS[DEFAULT_SCENARIO_ID]
    if scenario.id == requested:
        return scenario
    return replace(scenario, id=requested)


def pick_scenario(*, exclude_id: str | None = None) -> Scenario:
    """무작위로 고르되 직전 시나리오는 뺀다."""
    global _last_picked_id

    skip = exclude_id or _last_picked_id
    pool = [scenario for scenario in SCENARIOS.values() if scenario.id != skip]
    if not pool:
        pool = list(SCENARIOS.values())
    picked = choice(pool)
    _last_picked_id = picked.id
    return picked
