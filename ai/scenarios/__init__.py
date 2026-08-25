from __future__ import annotations

from ai.scenarios.card_payment_fraud import SCENARIO as CARD_PAYMENT_FRAUD
from ai.scenarios.institution_impersonation import SCENARIO as INSTITUTION_IMPERSONATION
from ai.scenarios.jsonl_loader import load_jsonl_scenarios
from ai.scenarios.loan_offer import SCENARIO as LOAN_OFFER
from ai.scenarios.types import Scenario

SCENARIOS: dict[str, Scenario] = {
    INSTITUTION_IMPERSONATION.id: INSTITUTION_IMPERSONATION,
    CARD_PAYMENT_FRAUD.id: CARD_PAYMENT_FRAUD,
    LOAN_OFFER.id: LOAN_OFFER,
}
SCENARIOS.update(load_jsonl_scenarios())


def get_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]
    except KeyError as exc:
        known = ", ".join(SCENARIOS)
        raise KeyError(f"Unknown scenario '{scenario_id}'. Expected one of: {known}") from exc


__all__ = ["SCENARIOS", "Scenario", "get_scenario"]
