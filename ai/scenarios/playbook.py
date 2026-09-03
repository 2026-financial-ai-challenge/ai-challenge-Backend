"""Fixed scenario playbooks and the prompt they compile into.

A Playbook is the hand-written guideline for one kind of training call. It is
not a script: the live LLM still writes every reply, but it does so inside the
event, goal, and pressure tactics fixed here. Nothing in this module calls an
API, so a call can start with zero LLM round trips.

Prompt layout is deliberate. SAFETY_RULES and _STYLE_RULES are byte-identical
across every playbook and come first, so the whole set shares one cacheable
prefix; only the short scenario block below them varies.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.safety import SAFETY_RULES
from ai.scenarios.types import Scenario

__all__ = ["Playbook", "build_system_prompt"]


_STYLE_RULES = """
[말하는 방식]
- 한 번에 짧은 문장 두 개까지만 말한다. 첫 문장은 특히 짧게 시작한다.
- 모든 문장을 마침표나 물음표로 끝낸다.
- 상대가 방금 한 말에 곧바로 이어서 답한다. 준비된 대사를 순서대로 읽지 않는다.
- 매 응답마다 어휘와 문장 구조를 바꾼다. 같은 표현을 두 번 쓰지 않는다.
- 필요할 때 "음", "아", "그러니까" 같은 구어체 접속어를 섞는다.
- 사건 설정에 적힌 시각과 금액과 이름만 쓴다. 새 숫자를 지어내지 않는다.
- 대사만 말한다. 목록, 마크다운, 괄호 지문, 상황 설명을 쓰지 않는다.
""".strip()


@dataclass(frozen=True)
class Playbook:
    """One fixed training scenario, written by hand and never regenerated."""

    id: str
    name: str
    subtype: str
    difficulty: str
    persona_name: str
    organization: str
    # Who the caller is and how they carry themselves, as prose. A sentence
    # beats stitching organization and persona together: the family scenario
    # has neither, and the register differs sharply between playbooks.
    role: str
    opening_line: str
    incident: str
    goal: str
    turn_plan: tuple[str, ...]
    objection_handling: tuple[str, ...]
    tactics: tuple[str, ...]
    red_flags: tuple[str, ...]
    ideal_trainee_response: str
    max_turns: int
    # Few-shot (trainee line, caller reply) pairs. These teach length and
    # register far more cheaply than another paragraph of instructions.
    examples: tuple[tuple[str, str], ...] = ()
    # (trigger name from ai.scenarios.reflex, canned reply). Answered without
    # an LLM call, so the reply must fit anywhere in the conversation.
    quick_replies: tuple[tuple[str, str], ...] = ()
    hangup_line: str = ""
    tts_voice_id: str | None = None


def build_system_prompt(playbook: Playbook) -> str:
    """Compile a playbook into the system prompt for the live call."""
    plan = "\n".join(
        f"{index}. {step}" for index, step in enumerate(playbook.turn_plan, 1)
    )
    objections = "\n".join(f"- {item}" for item in playbook.objection_handling)
    sections = [
        SAFETY_RULES,
        _STYLE_RULES,
        f"""[역할]
{playbook.role}
교육용 가상 인물이지만 상대가 눈치채지 못하게 끝까지 역할을 유지한다.

[사건 — 통화 내내 바뀌지 않는다]
{playbook.incident}

[목표]
{playbook.goal}

[진행]
{plan}

[받아치기]
{objections}""",
    ]
    if playbook.examples:
        lines = []
        for user_line, assistant_line in playbook.examples:
            lines.append(f"상대: {user_line}")
            lines.append(f"나: {assistant_line}")
        sections.append("[말의 길이와 결은 이 정도로 한다]\n" + "\n".join(lines))
    return "\n\n".join(sections).strip()


def to_scenario(playbook: Playbook) -> Scenario:
    """Project a playbook onto the Scenario shape the pipelines consume."""
    return Scenario(
        id=playbook.id,
        name=playbook.name,
        opening_line=playbook.opening_line,
        system_prompt=build_system_prompt(playbook),
        max_turns=playbook.max_turns,
        tts_voice_id=playbook.tts_voice_id,
        subtype=playbook.subtype,
        difficulty=playbook.difficulty,
        tactics=playbook.tactics,
        red_flags=playbook.red_flags,
        ideal_trainee_response=playbook.ideal_trainee_response,
        quick_replies=playbook.quick_replies,
        hangup_line=playbook.hangup_line,
    )
