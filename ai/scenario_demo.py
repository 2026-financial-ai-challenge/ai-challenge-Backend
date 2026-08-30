"""Throwaway dev script: show generate_scenario() output + review gate live.

Run from the repo root (needs OPENAI_API_KEY in ai/.env):

    python -m ai.scenario_demo
    (or: python ai/scenario_demo.py)

Not part of the app — delete anytime.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai.scenarios import get_scenario
from ai.scenarios.generator import generate_scenario


async def main() -> None:
    base = get_scenario("voice_phishing_training")
    for i in range(1, 4):
        print(f"\n{'='*60}\n  생성 {i}회차\n{'='*60}")
        try:
            scenario = await generate_scenario(base)
        except Exception as exc:
            print(f"  [실패] 3회 재시도 모두 실패 -> 폴백 시나리오로 대체됨: {exc}")
            continue
        print(f"  id             : {scenario.id}")
        print(f"  name           : {scenario.name}")
        print(f"  difficulty     : {scenario.difficulty}")
        print(f"  max_turns      : {scenario.max_turns}")
        print(f"  opening_line   : {scenario.opening_line}")
        print(f"  tactics        : {', '.join(scenario.tactics)}")
        print("  red_flags      :")
        for rf in scenario.red_flags:
            print(f"    - {rf}")
        print(f"  ideal_response : {scenario.ideal_trainee_response}")


if __name__ == "__main__":
    asyncio.run(main())
