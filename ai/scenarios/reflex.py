"""LLM-free instant replies for the handful of trainee lines that never vary.

Every live turn normally costs a full LLM round trip before the first TTS byte
can leave. A few trainee utterances ("안 들려요", "누구세요?") have an answer
that is fixed by the scenario, so answering them from a table skips the round
trip entirely.

This is deliberately conservative:
- Only high-confidence patterns are matched.
- Each trigger fires at most once per call, so the caller never repeats itself.
- A per-call budget keeps the conversation LLM-driven; the reflex table is a
  latency shortcut, not a dialogue engine.
"""

from __future__ import annotations

import re

__all__ = [
    "REFLEX_TRIGGERS",
    "ReflexTable",
    "match_trigger",
]

# Ordered: the first pattern that matches wins, so put the more specific
# intents (a scam accusation) ahead of the generic ones ("누구세요").
REFLEX_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        # "보이스피싱 아니에요?" — the moment where a real caller answers
        # instantly and a hesitating one gives itself away.
        "scam_accusation",
        re.compile(r"보이스\s*피싱|보이스피슁|피싱|사기\s*(전화|아니|치|꾼)|스팸"),
    ),
    (
        "not_audible",
        re.compile(r"안\s*들리|잘\s*안\s*들|소리가\s*(안|작)|목소리가\s*(안|작)|여보세요\s*여보세요"),
    ),
    (
        "repeat_that",
        re.compile(r"뭐라(고|구)(요|\?|$)|다시\s*(한번|한\s*번|말)|못\s*알아\s*들|무슨\s*말"),
    ),
    (
        "who_is_this",
        re.compile(r"누구(세|시|신|야|냐)|어디(세요|시죠|신데|에요|예요|야|서)|무슨\s*일|왜\s*전화"),
    ),
    (
        "busy_now",
        re.compile(r"바쁜|바빠|운전\s*중|회의\s*중|나중에\s*(통화|얘기)|시간\s*없"),
    ),
)


def match_trigger(text: str) -> str | None:
    """Return the canonical trigger name for a trainee utterance, or None."""
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    for name, pattern in REFLEX_TRIGGERS:
        if pattern.search(cleaned):
            return name
    return None


class ReflexTable:
    """Per-call bookkeeping over one scenario's quick replies.

    `quick_replies` is the scenario's ((trigger, reply), ...) tuple. `budget`
    caps how many turns in a whole call may be answered without the LLM.
    """

    def __init__(
        self,
        quick_replies: tuple[tuple[str, str], ...] | None,
        *,
        budget: int = 3,
    ) -> None:
        self._replies = {
            trigger: reply
            for trigger, reply in (quick_replies or ())
            if trigger and reply
        }
        self._budget = max(0, int(budget))
        self._used: set[str] = set()

    @property
    def remaining(self) -> int:
        return max(0, self._budget - len(self._used))

    def take(self, text: str) -> str | None:
        """Return a canned reply for `text`, consuming it, or None.

        Returning None means the turn must go to the LLM as usual.
        """
        if self.remaining <= 0:
            return None
        trigger = match_trigger(text)
        if trigger is None or trigger in self._used:
            return None
        reply = self._replies.get(trigger)
        if not reply:
            return None
        self._used.add(trigger)
        return reply
