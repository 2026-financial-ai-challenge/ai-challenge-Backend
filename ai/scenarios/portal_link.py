"""통화 도중 문자로 보내는 가상 사건 조회 포털 링크.

investigation_unit 통화가 연결되고 send_after_seconds 뒤에 백엔드(call_service.py)가 문자를 보낸다.
문자 문구는 백엔드 web_training_service.py에도 같은 문장이 있어야 한다(ai/tests/test_portal_link.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.scenarios import canonical_id


@dataclass(frozen=True)
class PortalLink:
    # 백엔드가 뒤에 링크를 붙여 보낸다
    sms_body: str
    # AI가 '문자를 보냈다'고 말하는 때에 맞춘다
    send_after_seconds: int


PORTAL_LINKS: dict[str, PortalLink] = {
    "investigation_unit": PortalLink(
        sms_body="[가온형사사법지원포털] 등기송달 열람 안내입니다. 본인 확인 후 열람하세요.",
        send_after_seconds=40,
    ),
}


def link_for(scenario_id: str) -> PortalLink | None:
    """시나리오에 이어지는 통화 중 링크. 없으면 None이고, 그 통화에는 문자를 보내지 않는다."""
    return PORTAL_LINKS.get(canonical_id(scenario_id))
