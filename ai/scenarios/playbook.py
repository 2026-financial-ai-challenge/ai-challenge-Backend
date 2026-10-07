"""시나리오 한 편(Playbook)과, 그것을 통화 지시문의 시나리오 블록으로 바꾸는 함수.

Playbook은 대본이 아니라 지침이다. 대사는 모델이 매번 새로 쓰고, 사건과 목표와 수법만 여기서 고정한다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.scenarios.types import ScriptReply

__all__ = ["Playbook", "ScriptReply", "build_scenario_block"]


@dataclass(frozen=True)
class Playbook:
    id: str
    # 누구이고 어떤 말투인지 문장으로 쓴다. 반말·존댓말도 여기서 정한다.
    role: str
    opening_line: str
    incident: str
    goal: str
    turn_plan: tuple[str, ...]
    objection_handling: tuple[str, ...]
    # 리포트 채점 기준(backend/app/services/report_service.py)
    tactics: tuple[str, ...]
    red_flags: tuple[str, ...]
    ideal_trainee_response: str
    max_turns: int
    # (상대 말, 내 답) 예시. 지시문을 더 쓰는 것보다 말의 길이와 결을 잘 잡는다.
    examples: tuple[tuple[str, str], ...] = ()
    # (상황 이름, 짧은 답). 녹취록에서 AI 화자를 찾는 기준 문장으로 쓴다(ai/transcript.py).
    quick_replies: tuple[tuple[str, str], ...] = ()
    hangup_line: str = ""
    # 거절했을 때 한 번 하는 포기 경고. 인물마다 손해나 불이익으로 압박한다.
    giveup_line: str = ""
    # 목표의 마지막 요구를 승낙받으면 하는 넘김 말. 이 말 뒤 상대가 한 번 더 말하면 통화를 끝낸다.
    handoff_line: str = ""
    # 단계별 대사 예시와 상대 반응별 받아치기 예시. CallContext에 참고용으로 들어가고
    # (ai/managed_agent.py), 녹취록에서 AI 화자를 찾는 기준 문장으로도 쓴다.
    progression: tuple[str, ...] = ()
    script: tuple[ScriptReply, ...] = ()


def build_scenario_block(playbook: Playbook) -> str:
    """통화마다 CallContext로 보내는 시나리오 부분. 공통 규칙은 에이전트에 있다(ai/managed_agent.py)."""
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

