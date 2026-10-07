"""고정 시나리오 조회와 선택. 시나리오 내용은 library.py에 있다."""

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

# 예전 id. 백엔드 기본값과 예전 통화 기록이 계속 찾을 수 있게 남긴다.
_ALIASES = {
    "voice_phishing_training": DEFAULT_SCENARIO_ID,
    "delivery_payment_error": "ipo_allocation",
    "family_emergency": "card_delivery",
}

# 직전에 고른 시나리오. 프로세스 안에서만 연달아 겹치지 않게 한다.
_last_picked_id: str | None = None


def canonical_id(scenario_id: str) -> str:
    """별칭을 지금 id로 바꾼다. get_scenario()는 요청한 id를 그대로 남기므로 id 비교에는 이것을 쓴다."""
    requested = (scenario_id or "").strip()
    return _ALIASES.get(requested, requested)


def get_scenario(scenario_id: str) -> Scenario:
    """id로 시나리오를 찾는다. 모르는 id는 기본 시나리오로 넘기되 요청한 id는 유지한다."""
    requested = (scenario_id or "").strip() or DEFAULT_SCENARIO_ID
    key = canonical_id(requested)
    scenario = SCENARIOS.get(key)
    if scenario is None:
        scenario = SCENARIOS[DEFAULT_SCENARIO_ID]
    if scenario.id == requested:
        return scenario
    return replace(scenario, id=requested)


def pick_scenario(*, exclude_id: str | None = None) -> Scenario:
    """통화 한 건의 시나리오를 무작위로 고른다. 직전 시나리오는 뺀다."""
    global _last_picked_id

    skip = exclude_id or _last_picked_id
    pool = [scenario for scenario in SCENARIOS.values() if scenario.id != skip]
    if not pool:
        pool = list(SCENARIOS.values())
    picked = choice(pool)
    _last_picked_id = picked.id
    return picked
