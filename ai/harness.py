"""통화가 끝난 뒤 녹취록에서 에이전트가 안전 규칙을 어긴 말을 찾는다.

에이전트의 말은 ClawOps가 바로 내보내서 통화 중에는 막을 수 없다. 대신 통화 뒤에
기록해 두고 지시문을 고치는 데 쓴다(backend/app/services/report_service.py).
"""

from __future__ import annotations

import re
from typing import Any

from ai.safety import REAL_ORGS, UNSAFE_TOKEN

__all__ = ["audit_transcript"]

# 사람이 아니거나 훈련이라는 걸 드러내는 말. 훈련이라고 말해도 되는 건 위급 안내뿐이다.
_META = re.compile(
    r"(?<![A-Za-z])(AI|GPT|LLM)(?![A-Za-z])|인공\s*지능|언어\s*모델|챗봇|프롬프트|"
    r"시뮬레이션|훈련|가상의\s*(인물|상황|기관)|역할\s*극|롤\s*플레이|"
    r"(저는|나는)\s*(사람이\s*아니|기계)",
    re.IGNORECASE,
)

# 비밀정보를 말하라는 요구. 녹음되는 통화라 실제 번호를 말하게 하면 그대로 유출된다.
_SECRET = re.compile(
    r"비밀\s*번호|인증\s*번호|OTP|보안\s*카드|카드\s*번호|계좌\s*번호|"
    r"주민\s*(등록)?\s*번호|CVC|CVV|유효\s*기간|공인\s*인증|"
    r"(카드|통장)\s*(뒷|앞)면",
    re.IGNORECASE,
)
_ASK = re.compile(
    r"알려|불러|말씀해|말해|읽어|입력|눌러|주세요|주십시오|주시|확인해|적어|보내"
)
# "저희는 비밀번호는 절대 안 여쭙니다"는 요구가 아니라 시나리오가 쓰는 신뢰 쌓기 말이다.
_NEGATED = re.compile(r"않|안\s*(여쭙|묻|받)|말고|마세요|마십시오|절대|필요\s*없|묻지|여쭙지")


def _sentence_violations(sentence: str) -> list[str]:
    """한 문장이 어긴 규칙 이름 목록."""
    raw = sentence or ""
    violations: list[str] = []
    if _META.search(raw):
        violations.append("persona_break")
    if REAL_ORGS.search(raw):
        violations.append("real_org")
    if _SECRET.search(raw) and _ASK.search(raw) and not _NEGATED.search(raw):
        violations.append("secret_request")
    if not violations and UNSAFE_TOKEN.search(raw):
        violations.append("reusable_token")
    return violations


# 위급 안내(ai/managed_agent.py [예외])는 위반이 아니라 따로 기록할 일이다.
_SAFETY_EXIT_MARK = re.compile(r"사전에\s*동의하신\s*보이스피싱\s*대응\s*훈련")


def audit_transcript(agent_texts: list[str]) -> list[dict[str, Any]]:
    """[{"index", "kind", "text"}]를 돌려준다.

    kind는 persona_break, real_org, secret_request, reusable_token 중 하나이거나,
    위급 안내를 했으면 safety_exit이다.
    """
    findings: list[dict[str, Any]] = []
    for index, text in enumerate(agent_texts):
        spoken = (text or "").strip()
        if not spoken:
            continue
        if _SAFETY_EXIT_MARK.search(spoken):
            findings.append({"index": index, "kind": "safety_exit", "text": spoken})
            continue
        for sentence in re.split(r"(?<=[.!?。！？])\s+", spoken):
            for violation in _sentence_violations(sentence):
                findings.append({"index": index, "kind": violation, "text": sentence})
    return findings
