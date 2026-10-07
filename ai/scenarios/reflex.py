""""안 들려요", "누구세요?"처럼 답이 정해진 짧은 말에 고정 답을 고르는 표.

예전 대본 모드 코드. 현재 통화에서는 안 쓰고 backend/tests 때문에 남겨 둠.
"""

from __future__ import annotations

import re

__all__ = [
    "MAX_REFLEX_CHARS",
    "REFLEX_TRIGGERS",
    "ReflexTable",
    "match_trigger",
]

# 긴 문장 속 같은 단어는 의도가 달라서 짧은 발화만 매칭
MAX_REFLEX_CHARS = 40

# 앞에서부터 먼저 맞는 것을 쓴다. 구체적인 패턴을 앞에.
REFLEX_TRIGGERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
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
        re.compile(r"누구(세|시|신|야|냐)|어디(세요|시죠|신데|에요|예요|야|서|라고)|무슨\s*일|왜\s*전화"),
    ),
    (
        "busy_now",
        re.compile(r"바쁜|바빠|운전\s*중|회의\s*중|나중에\s*(통화|얘기)|시간\s*없"),
    ),
)


def match_trigger(text: str) -> str | None:
    """훈련자 발화의 트리거 이름. 없으면 None."""
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > MAX_REFLEX_CHARS:
        return None
    for name, pattern in REFLEX_TRIGGERS:
        if pattern.search(cleaned):
            return name
    return None


class ReflexTable:
    """통화별 고정 답 사용 기록. budget: 통화당 고정 답 최대 횟수."""

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
        """고정 답을 꺼내 쓴다. None이면 모델이 답할 차례."""
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
