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
    tts_stability: float = 0.4
    tts_similarity_boost: float = 0.75
