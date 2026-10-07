from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    """백엔드용 시나리오. Playbook에서 to_scenario()로 만든다."""

    id: str
    name: str
    opening_line: str
    system_prompt: str
    max_turns: int
    tts_voice_id: str | None = None
    subtype: str | None = None
    difficulty: str | None = None
    tactics: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    ideal_trainee_response: str | None = None
    # 녹취 화자 판정용(ai/transcript.py)
    quick_replies: tuple[tuple[str, str], ...] = ()
    hangup_line: str = ""
    progression: tuple[str, ...] = ()
    script: tuple = ()
