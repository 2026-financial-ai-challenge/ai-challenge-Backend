"""통화 도중 문자로 보내는 가상 사건 조회 포털 링크(통화 중 발송안).

investigation_unit 통화 중에 sms_body 뒤에 링크를 붙여 훈련자에게 문자로 보낸다. 통화 대사(library.py)도
'방금 문자를 보냈다'로 맞춰져 있다. 훈련자가 링크에서 개인정보를 제출하면 프론트가 경고를 띄우고,
백엔드가 같은 순간 통화도 끊는다(ends_call).

백엔드(call_service.py)는 통화가 연결되면 send_after_seconds 뒤에 문자를 보낸다(통화가 그 전에 끝나면 종료 후).
문구는 아직 web_training_service.py의 자체 문구를 쓰고, 위험 행동 때 통화를 끊는 것(ends_call)도 아직 없다.
링크 경로와 이벤트 이름은 백엔드의 /t/{token}, WEB_EVENT_TYPES를 기준으로 한다(아래 값은 참고용).

문장은 시나리오 문장과 같은 안전 기준을 지킨다(ai/tests/test_portal_link.py).
'훈련용' 표시와 경고 문구는 프론트가 화면에 따로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.scenarios import canonical_id

# 프론트가 백엔드에 보내는 행동. 값은 보고서에 쓸 설명이다.
PORTAL_EVENTS: dict[str, str] = {
    "opened": "문자 링크를 열었다",
    "verify_opened": "실명 인증 화면을 열었다",
    "inquiry_submitted": "사건번호와 성명을 입력하고 조회하기를 눌렀다",
    "verify_submitted": "성명, 생년월일, 휴대전화번호를 입력하고 실명 인증하기를 눌렀다",
    "left": "아무것도 제출하지 않고 페이지를 닫았다",
}
# 개인정보를 '제출'하는 버튼을 누른 순간만 위험 행동이다. 이 행동이 기록되면 경고를 띄우고
# 링크를 닫고 통화도 끊는다. 페이지를 열거나 화면을 옮기거나 입력칸에 글자를 치는 것만으로는
# 경고하지 않는다(그냥 둘러본 사람과 정보를 낸 사람을 가르기 위해서다).
TERMINAL_EVENTS = ("inquiry_submitted", "verify_submitted")

# 경고 화면 제목. 프론트가 같은 문구를 화면에 둔다.
WARNING_TITLE = "보이스피싱에 당했습니다"


@dataclass(frozen=True)
class PortalLink:
    scenario_id: str
    # 백엔드가 이 뒤에 링크를 붙여 보낸다
    sms_body: str
    # 통화가 연결된 지 이만큼 지난 뒤에 보낸다. AI가 '방금 문자를 보냈다'고 말하는 때와 맞춘다.
    send_after_seconds: int
    # 위험 행동이 기록되면 통화도 끊는다
    ends_call: bool
    # 행동별 경고 화면 설명
    warnings: dict[str, str]


PORTAL_LINKS: dict[str, PortalLink] = {
    "investigation_unit": PortalLink(
        scenario_id="investigation_unit",
        sms_body=(
            "[금융범죄 합동대응반] 자산보전과 서재욱 조사관입니다.\n"
            "방금 통화드린 사건의 열람 안내입니다. 가온형사사법지원포털 '나의 사건 조회'에서 성함으로 확인해 주세요."
        ),
        send_after_seconds=40,
        ends_call=True,
        warnings={
            "inquiry_submitted": (
                "수사기관은 전화나 문자로 링크를 보내 사건을 조회하게 하지 않습니다. "
                "이런 링크에 이름 같은 개인정보를 입력하면 신원 확인용 정보가 사기범에게 넘어갑니다. "
                "실제로 입력한 정보는 어디로도 전송되지 않았습니다."
            ),
            "verify_submitted": (
                "수사기관이 실명 인증을 하라며 링크를 보내는 일은 없습니다. "
                "성명, 생년월일, 휴대전화번호가 한꺼번에 넘어가면 명의 도용과 추가 사기에 쓰입니다. "
                "실제로 입력한 정보는 어디로도 전송되지 않았습니다."
            ),
        },
    ),
}


def link_for(scenario_id: str) -> PortalLink | None:
    """시나리오에 이어지는 통화 중 링크. 없으면 None이고, 그 통화에는 문자를 보내지 않는다."""
    return PORTAL_LINKS.get(canonical_id(scenario_id))
