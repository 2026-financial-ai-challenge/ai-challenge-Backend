from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptReply:
    """상대 반응(intent) 하나에 대한 받아치기 예시 문장들."""

    intent: str
    lines: tuple[str, ...]
