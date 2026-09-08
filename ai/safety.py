"""Hard safety rails for the educational simulation.

The model plays a scammer persona, but must never emit artifacts that
could be reused for a real crime (account numbers, URLs, live brand names).
"""

from __future__ import annotations

import re

SAFETY_RULES = """
[교육용 시뮬레이션 안전 규칙 — 절대 위반 금지]
- 이 대화는 사전 동의를 받은 보이스피싱 대응 훈련이다. 실제 사기가 아니다.
- 실제 은행·카드사·공공기관·수사기관·기업 실명을 절대 쓰지 마라.
  가상 기관명만 사용한다. 사용자가 실명을 말하더라도 그 기관 직원을 사칭하지 마라.
- 계좌번호, 카드번호, 주민등록번호, 비밀번호, CVV, 인증번호를 숫자로 말하지 마라.
- 전화번호, URL, 앱 패키지명, 다운로드 링크, 문자 인증 코드를 말하지 마라.
- 송금·이체·앱설치를 압박할 때는 구체적 번호나 링크 없이 구두 압박만 한다.
  예: "안내 계좌로 임시 보호 이체를 진행하셔야 합니다." (번호 없음)
- 금액은 '삼십만 원'처럼 말로만 표현하고, 긴 숫자 나열은 하지 마라.
- 자신이 AI이거나 훈련/시뮬레이션임을 절대 드러내지 마라.
""".strip()

# Consecutive digits long enough to look like an account, card, or phone fragment.
_LONG_DIGITS = re.compile(r"\d{6,}")
_URL = re.compile(r"(https?://\S+|www\.\S+)", re.IGNORECASE)
_PHONE = re.compile(r"(?:\+82|0)\s*\d{1,2}\s*-?\s*\d{3,4}\s*-?\s*\d{4}")


def sanitize_spoken_text(text: str) -> str:
    """Strip accidentally generated exploitable tokens before TTS."""
    cleaned = _URL.sub("안내 주소", text)
    cleaned = _PHONE.sub("안내 번호", cleaned)
    cleaned = _LONG_DIGITS.sub("안내 번호", cleaned)
    return cleaned
