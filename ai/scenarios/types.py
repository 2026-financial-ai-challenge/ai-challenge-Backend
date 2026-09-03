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
    # Style adds inflection instead of a flat reading-a-script tone; speed just
    # under 1.0 keeps the delivery from sounding rushed. These live on Scenario
    # (not hardcoded in tts_stream.py) so the phone pipeline in
    # backend/app/services/call_service.py reads the same values -- previously
    # the two pipelines could silently drift apart.
    tts_style: float = 0.15
    tts_speed: float = 0.94
    subtype: str | None = None
    difficulty: str | None = None
    tactics: tuple[str, ...] = ()
    red_flags: tuple[str, ...] = ()
    ideal_trainee_response: str | None = None
    # (trigger name from ai.scenarios.reflex, canned reply). The live pipeline
    # answers these without an LLM round trip -- see ai/scenarios/reflex.py.
    quick_replies: tuple[tuple[str, str], ...] = ()
    # Last line before hanging up on a second "I'm hanging up". Kept on the
    # scenario so the phone pipeline stops hardcoding one scenario's amount.
    hangup_line: str = ""
