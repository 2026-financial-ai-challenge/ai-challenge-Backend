"""Fixed training scenarios.

Scenarios are hand-written and live in ai/scenarios/library.py. Looking one up
costs no network call, so a training call no longer waits on an LLM before the
phone rings. The live in-call LLM is the only thing that still hits an API key,
and it treats the scenario as its guideline rather than a script.

ai/scenarios/generator.py can still write a scenario with an LLM, but only
when DYNAMIC_SCENARIO is turned on.
"""

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
    "get_scenario",
    "pick_scenario",
]

DEFAULT_SCENARIO_ID = DEFAULT_PLAYBOOK_ID

SCENARIOS: dict[str, Scenario] = {
    playbook.id: to_scenario(playbook) for playbook in PLAYBOOKS
}

# Historical id used by CALL_SCENARIO and by the backend default. Kept so an
# existing deployment keeps working without an env change.
_ALIASES = {"voice_phishing_training": DEFAULT_SCENARIO_ID}

# Which scenario the last pick_scenario() handed out, so back-to-back training
# calls in one process do not repeat themselves.
_last_picked_id: str | None = None


def get_scenario(scenario_id: str) -> Scenario:
    """Look a scenario up by id.

    An unknown id falls back to the default scenario but keeps the requested
    id, so CALL_SCENARIO can name a training type that has no playbook yet
    without breaking the call.
    """
    requested = (scenario_id or "").strip() or DEFAULT_SCENARIO_ID
    key = _ALIASES.get(requested, requested)
    scenario = SCENARIOS.get(key)
    if scenario is None:
        scenario = SCENARIOS[DEFAULT_SCENARIO_ID]
    if scenario.id == requested:
        return scenario
    return replace(scenario, id=requested)


def pick_scenario(*, exclude_id: str | None = None) -> Scenario:
    """Pick a scenario for one training call, avoiding an immediate repeat."""
    global _last_picked_id

    skip = exclude_id or _last_picked_id
    pool = [scenario for scenario in SCENARIOS.values() if scenario.id != skip]
    if not pool:
        pool = list(SCENARIOS.values())
    picked = choice(pool)
    _last_picked_id = picked.id
    return picked
