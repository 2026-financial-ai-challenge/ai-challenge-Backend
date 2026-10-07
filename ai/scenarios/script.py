"""훈련자 의도에 맞는 미리 쓴 대사를 고른다. ScriptReply는 Playbook.script 타입이라 계속 쓴다.

예전 대본 모드 코드. 현재 통화에서는 안 쓰고 backend/tests 때문에 남겨 둠.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai.scenarios.intents import ACK, IntentMatch, classify

__all__ = ["RouteDecision", "ScriptReply", "ScriptRouter"]


@dataclass(frozen=True)
class ScriptReply:
    """훈련자 의도 하나에 대한 대사 후보."""

    intent: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RouteDecision:
    """발화별 라우팅 결과.

    reason: hit(line으로 답함) | no_intent | ambiguous | long | no_line | exhausted(후보를 다 씀)
    """

    text: str
    intent: str | None
    reason: str
    line: str | None = None
    matched: tuple[str, ...] = field(default=())

    @property
    def hit(self) -> bool:
        return self.reason == "hit" and bool(self.line)

    def as_log(self) -> dict:
        return {
            "text": self.text,
            "intent": self.intent,
            "matched": list(self.matched),
            "reason": self.reason,
            "hit": self.hit,
            "line": self.line,
        }


class ScriptRouter:
    """통화별 대사 사용 기록. 같은 대사는 두 번 쓰지 않는다."""

    def __init__(
        self,
        *,
        progression: tuple[str, ...] = (),
        replies: tuple[ScriptReply, ...] = (),
        quick_replies: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._progression = tuple(line for line in progression if line.strip())
        self._next_step = 0
        lines: dict[str, list[str]] = {}
        # quick_replies도 같은 의도의 답이라 후보에 포함
        for trigger, reply in quick_replies or ():
            if trigger and reply.strip():
                lines.setdefault(trigger, []).append(reply.strip())
        for reply in replies or ():
            for line in reply.lines:
                if line.strip() and line.strip() not in lines.get(reply.intent, []):
                    lines.setdefault(reply.intent, []).append(line.strip())
        self._lines = lines
        self._used: set[str] = set()

    @classmethod
    def for_scenario(cls, scenario) -> "ScriptRouter":
        return cls(
            progression=tuple(getattr(scenario, "progression", ()) or ()),
            replies=tuple(getattr(scenario, "script", ()) or ()),
            quick_replies=tuple(getattr(scenario, "quick_replies", ()) or ()),
        )

    def peek(self, text: str) -> RouteDecision:
        """대사를 쓰지 않고 판단만 한다."""
        return self._decide(classify(text), consume=False)

    def route(self, text: str) -> RouteDecision:
        """판단 후 고른 대사를 사용 처리한다."""
        return self._decide(classify(text), consume=True)

    def _decide(self, match: IntentMatch, *, consume: bool) -> RouteDecision:
        base = {"text": match.text, "intent": match.intent, "matched": match.matched}
        if match.intent is None:
            return RouteDecision(reason="no_intent", **base)
        if not match.routable_length:
            return RouteDecision(reason="long", **base)
        if match.ambiguous:
            return RouteDecision(reason="ambiguous", **base)

        if match.intent == ACK:
            if self._next_step >= len(self._progression):
                return RouteDecision(reason="exhausted" if self._progression else "no_line", **base)
            line = self._progression[self._next_step]
            if consume:
                self._next_step += 1
                self._used.add(line)
            return RouteDecision(reason="hit", line=line, **base)

        variants = self._lines.get(match.intent) or []
        if not variants:
            return RouteDecision(reason="no_line", **base)
        for line in variants:
            if line not in self._used:
                if consume:
                    self._used.add(line)
                return RouteDecision(reason="hit", line=line, **base)
        return RouteDecision(reason="exhausted", **base)
