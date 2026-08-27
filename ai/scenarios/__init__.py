from __future__ import annotations

from ai.scenarios.types import Scenario

SCENARIOS: dict[str, Scenario] = {}


def get_scenario(scenario_id: str) -> Scenario:
    return Scenario(
        id="dynamic_seed",
        name=scenario_id.strip() or "voice_phishing_training",
        opening_line="안녕하세요. 확인할 거래 건으로 연락드렸습니다.",
        system_prompt="동적 시나리오 생성을 위한 기본 시드입니다.",
        max_turns=8,
    )


__all__ = ["SCENARIOS", "Scenario", "get_scenario"]
