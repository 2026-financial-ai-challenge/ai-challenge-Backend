"""훈련자의 종료 의사 판정. 리포트 간이 채점(report_service.heuristic_report)용.

통화 중 AI가 끊는 시점은 managed_agent의 전화 규칙이 정한다(여기 아님).
"""

from __future__ import annotations

import re

__all__ = ["HANG_UP", "HANG_UP_TAIL_CHARS", "wants_hang_up"]

# 거절("안 할래요", "됐어요")은 종료 의사가 아니라서 뺐다.
# "이만"은 뒤에 종료 표현이 붙을 때만 인정("이만 삼천 원" 오탐 방지).
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


# 발화 끝 30자 안에서만 인정. 앞부분의 같은 표현은 대개 인용이다("그 사람이 전화 끊으라던데요").
HANG_UP_TAIL_CHARS = 30


def wants_hang_up(text: str) -> bool:
    cleaned = text or ""
    if not cleaned:
        return False
    tail_start = len(cleaned) - HANG_UP_TAIL_CHARS
    return any(match.end() >= tail_start for match in HANG_UP.finditer(cleaned))
