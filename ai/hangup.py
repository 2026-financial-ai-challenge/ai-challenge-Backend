"""Detecting that the trainee wants to end the call.

This drives two things that must agree: the caller's behaviour (hold on to
the first attempt, hang up on the second) and the report's scoring, where
"전화 종료(빠른 판단)" is one of the highest-weighted defensive behaviours.
The pattern used to be copy-pasted into both call sites, so widening one and
not the other would have silently changed the score without changing the
conversation. One definition, imported by both.
"""

from __future__ import annotations

import re

__all__ = ["HANG_UP", "HANG_UP_TAIL_CHARS", "wants_hang_up"]

# Deliberately conservative: every alternative below is terminal on a phone
# call. Refusals ("안 할래요", "됐어요") are NOT included -- they mean "no" to
# the request, not "I am ending this call", and the scenarios rely on that
# distinction to keep pushing.
#
# "이만" only counts when followed by a closing verb: on its own it is the
# number 20,000, and delivery_payment_error's incident is 이만 삼천 원.
HANG_UP = re.compile(
    r"끊겠|끊을게|끊습니다|끊는다|전화\s*끊|"
    r"끝낼|끝내겠|끝내죠|끝냅니다|"
    r"그만하세요|그만할|그만하죠|그만하겠|그만둘|그만 전화|"
    r"통화\s*(그만|종료|끝)|"
    r"더\s*이상\s*(통화|얘기|말)|"
    r"이만\s*(끊|실례|줄이|가)|"
    r"나중에\s*걸|"
    r"수고하세요|수고하십시오"
)


# Announcing the end of a call is the last thing someone says. The same words
# earlier in a long answer are almost always reported speech -- "그 사람이 전화
# 끊으라고 하던데요" -- and the trainee carries straight on talking past them.
# Requiring the match to land in the tail costs nothing on a short utterance:
# anything shorter than this window is all tail, so every one-line closing
# behaves exactly as it did before.
#
# This matters more than it used to. PhonePipelineSession now merges a turn
# that Deepgram split across several finals back into one utterance, so the
# text this sees is a whole answer rather than a fragment of one.
HANG_UP_TAIL_CHARS = 30


def wants_hang_up(text: str) -> bool:
    cleaned = text or ""
    if not cleaned:
        return False
    tail_start = len(cleaned) - HANG_UP_TAIL_CHARS
    return any(match.end() >= tail_start for match in HANG_UP.finditer(cleaned))
