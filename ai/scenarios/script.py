"""Answering a trainee turn from pre-written lines instead of the live LLM.

This is the core of the "script" call mode. A scenario ships two kinds of
pre-written caller lines:

- `progression`: the backbone of the call, in order. Each time the trainee
  goes along with it ("네", "그런데요", their name), the next line is spoken.
  This is how a real caller reading from a script moves a victim step by step.
- `script`: answers keyed by trainee intent (ai/scenarios/intents.py) --
  what to say to "누구세요", "다시 걸게요", "개인정보라 안 돼요" and so on.
  Each intent has a few interchangeable variants.

Because every line is known before the call, its audio can be synthesized in
advance at full quality (ai/prerender.py) and played the moment the trainee
stops talking. Anything the router is not sure about returns a miss, and the
live LLM answers that turn exactly as before.

A miss is never an error. It is the router saying "this one needs listening",
and the ratio of hits to turns is the number `python -m ai.script_eval` reports.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ai.scenarios.intents import ACK, IntentMatch, classify

__all__ = ["RouteDecision", "ScriptReply", "ScriptRouter"]


@dataclass(frozen=True)
class ScriptReply:
    """Interchangeable caller lines for one trainee intent."""

    intent: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RouteDecision:
    """What the router did with one trainee turn, and why.

    `reason` is one of:
      hit        -- `line` is the reply
      no_intent  -- nothing matched
      ambiguous  -- two different intents matched
      long       -- too long to answer with a fixed line
      no_line    -- the intent is known but this scenario has no line for it
      exhausted  -- every variant was already used in this call
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
    """Per-call state over one scenario's pre-written lines.

    Never repeats a line within a call: a caller who says the exact same
    sentence twice is the fastest way to sound recorded. Once an intent's
    variants are spent, that intent goes to the LLM for the rest of the call.
    """

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
        # Quick replies are the reflex table's one-liners. They answer the same
        # intents ("누구세요", "안 들려요"), so they join the variant pool.
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

    @property
    def has_script(self) -> bool:
        return bool(self._progression or self._lines)

    @property
    def remaining_progression(self) -> int:
        return max(0, len(self._progression) - self._next_step)

    def peek(self, text: str) -> RouteDecision:
        """Decide without consuming anything (shadow mode, evaluation)."""
        return self._decide(classify(text), consume=False)

    def route(self, text: str) -> RouteDecision:
        """Decide and consume the chosen line."""
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

    def all_lines(self) -> tuple[str, ...]:
        """Every line this router can speak -- what prerendering has to cover."""
        seen: list[str] = []
        for line in (*self._progression, *(l for ls in self._lines.values() for l in ls)):
            if line not in seen:
                seen.append(line)
        return tuple(seen)
