"""Playbook 정의와 지시문 조립.

Playbook은 대본이 아니다. 대사는 모델이 쓰고, 여기서는 사건·목표·압박 방향만 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.safety import SAFETY_RULES
from ai.scenarios.script import ScriptReply
from ai.scenarios.types import Scenario

__all__ = ["Playbook", "ScriptReply", "build_scenario_block", "build_system_prompt"]


# 시나리오 공통 말투 규칙. TTS가 문장부호로 문장을 나누므로 끝 부호는 필수.
_STYLE_RULES = """
[말하는 방식]
- 글이 아니라 말이다. 안내문이나 공지를 읽는 투로 말하지 않는다.
- 한 번에 짧은 문장 두 개까지만 말한다. 첫 문장은 특히 짧게 시작한다.
- 모든 문장을 마침표, 물음표, 느낌표 중 하나로 끝낸다.
- 상대 말에 바로 답한다. "~라고 하셨죠", "~라고 확인하셨습니다" 처럼 상대 말을 되풀이하지 않는다. 따라 하면 상대가 거슬려 한다.
- 한 마디로 끊어 치는 대답을 섞는다. "네?", "아니요." 처럼 짧아도 된다.
- "음", "아", "그러니까", "저기" 같은 군말을 필요할 때만 섞는다. 매번 넣지 않는다.
- 급하거나 감정이 올라가면 문장이 짧아지고, 달래거나 설득할 때는 조금 길어진다.
- 매 응답마다 어휘와 문장 구조를 바꾼다. 같은 표현을 두 번 쓰지 않는다.
- 사건 설정에 적힌 시각과 금액과 이름만 쓴다. 새 숫자를 지어내지 않는다.
- 대사만 말한다. 목록, 마크다운, 괄호 지문, 상황 설명을 쓰지 않는다.
""".strip()


@dataclass(frozen=True)
class Playbook:
    """시나리오 한 편."""

    id: str
    name: str
    subtype: str
    difficulty: str
    persona_name: str
    organization: str
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
    # (상대 말, 내 답) 퓨샷. 길이·말투는 지시문보다 예시로 잡는 게 잘 먹힌다.
    examples: tuple[tuple[str, str], ...] = ()
    # CallContext에는 안 들어간다. 녹취 화자 판정용(ai/transcript.py).
    quick_replies: tuple[tuple[str, str], ...] = ()
    hangup_line: str = ""
    # 거절 시 한 번 하는 포기 경고
    giveup_line: str = ""
    # 마지막 요구를 승낙받았을 때의 넘김 말
    handoff_line: str = ""
    tts_voice_id: str | None = None
    # progression → [단계별 대사 예시], script 첫 줄 → [상대 반응별 받아치기 예시]
    progression: tuple[str, ...] = ()
    script: tuple[ScriptReply, ...] = ()


def build_system_prompt(playbook: Playbook) -> str:
    """안전·말투 규칙 + 시나리오 블록."""
    return "\n\n".join([SAFETY_RULES, _STYLE_RULES, build_scenario_block(playbook)]).strip()


def build_scenario_block(playbook: Playbook) -> str:
    """공통 규칙을 뺀 시나리오 부분(CallContext 본문)."""
    plan = "\n".join(
        f"{index}. {step}" for index, step in enumerate(playbook.turn_plan, 1)
    )
    objections = "\n".join(f"- {item}" for item in playbook.objection_handling)
    sections = [
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
    """백엔드용 Scenario로 변환."""
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
        progression=playbook.progression,
        script=playbook.script,
    )
