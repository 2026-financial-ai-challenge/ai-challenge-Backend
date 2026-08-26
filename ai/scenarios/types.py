from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    opening_line: str
    system_prompt: str
    max_turns: int
    tts_voice_id: str | None = None
    tts_stability: float = 0.3
    tts_similarity_boost: float = 0.35
    subtype: str | None = None
    difficulty: str | None = None
    tactics: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    ideal_trainee_response: str | None = None
