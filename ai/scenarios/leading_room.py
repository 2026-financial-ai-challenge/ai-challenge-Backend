"""통화가 끝난 뒤 이어지는 가상 리딩방.

ipo_allocation 통화가 끝나면 백엔드가 sms_body 뒤에 링크를 붙여 훈련자에게 문자로 보낸다.
링크를 열면 프론트가 이 대본대로 메신저 단체방 화면을 보여 준다. 위험 행동 버튼을 누르면
실제 행동이 일어나기 직전에 경고를 띄우고 링크를 닫는다. 프론트는 ROOM_EVENTS의 행동을
백엔드에 보내고, 백엔드는 이 기록을 리포트 점수에 반영한다(점수 기준은 아직 정하지 않음).

대본 문장은 시나리오 문장과 같은 안전 기준을 지킨다(ai/tests/test_leading_room.py):
실제 기관명, URL, 여섯 자리 이상 숫자를 쓰지 않는다. 문자와 방 안 메시지에는 '훈련'이라고 쓰지 않는다.
'훈련용' 표시와 경고 문구는 프론트가 화면에 따로 둔다.
"""

from __future__ import annotations

from dataclasses import dataclass

from ai.scenarios import canonical_id

# 프론트가 백엔드에 보내는 행동. 값은 보고서에 쓸 설명이다.
ROOM_EVENTS: dict[str, str] = {
    "opened": "문자 링크를 열었다",
    "joined": "리딩방에 입장했다",
    "app_install": "전용 거래 앱 받기를 눌렀다",
    "inquiry": "1:1 상담 신청을 눌렀다",
    "deposit": "배정 신청(입금)을 눌렀다",
    "declined": "입장하지 않거나 방을 나갔다",
}
# 이 행동을 누르면 실제로 일어나기 직전에 경고를 띄우고 링크를 닫는다.
TERMINAL_EVENTS = ("app_install", "inquiry", "deposit")

WARNING_TITLE = "여기서 보이스피싱 피해가 시작됩니다"


@dataclass(frozen=True)
class RoomMessage:
    sender: str
    text: str
    # 앞 메시지가 뜬 뒤 이 메시지가 뜨기까지 기다리는 시간
    delay_ms: int
    is_manager: bool = False


@dataclass(frozen=True)
class RoomAction:
    event: str  # TERMINAL_EVENTS 중 하나
    label: str  # 버튼 문구
    warning: str  # 누른 뒤 경고 화면에 보일 설명


@dataclass(frozen=True)
class LeadingRoom:
    scenario_id: str
    # 백엔드가 이 뒤에 링크를 붙여 보낸다
    sms_body: str
    room_title: str
    member_count: int
    # 입장 전 초대 화면 문구
    invite_text: str
    # 방은 공지방이라 일반 참여자는 글을 쓸 수 없다. 입력창 대신 이 문구를 보인다.
    input_notice: str
    messages: tuple[RoomMessage, ...]
    actions: tuple[RoomAction, ...]


_MANAGER = "한지수 매니저"

LEADING_ROOMS: dict[str, LeadingRoom] = {
    "ipo_allocation": LeadingRoom(
        scenario_id="ipo_allocation",
        sms_body=(
            "[누리솔투자자문] 고객님, 방금 통화드린 한지수 매니저입니다.\n"
            "공모주 기관 물량 우선 배정방 입장 링크 보내드려요. 배정 마감 세 시간 전이니 서둘러 주세요."
        ),
        room_title="누리솔 VIP 공모주 우선배정방",
        member_count=327,
        invite_text="한지수 매니저님이 '누리솔 VIP 공모주 우선배정방'에 초대했습니다.",
        input_notice="공지방입니다. 문의는 1:1 상담으로 남겨 주세요.",
        messages=(
            RoomMessage(_MANAGER, "새로 오신 고객님들 환영합니다. 오늘 기관 배정 물량 안내드릴게요.", 800, True),
            RoomMessage(_MANAGER, "다음 주 상장 바이오 기업, 공모가 대비 30% 할인 기관 물량입니다. 원금은 회사가 보장합니다.", 1800, True),
            RoomMessage("박*숙", "지난달 배정 받았는데 상장 첫날 두 배 넘게 올랐어요. 매니저님 감사합니다!", 2200),
            RoomMessage("김*호", "저도 수익 인증합니다. 500만 원 넣고 이틀 만에 1,040만 원 됐네요.", 2000),
            RoomMessage("이*영", "처음엔 반신반의했는데 원금 보장이라 안심하고 넣었어요. 이번에도 신청합니다.", 2400),
            RoomMessage(_MANAGER, "이번 물량은 증권사 앱에는 안 나옵니다. 전용 거래 앱으로만 신청받아요.", 2000, True),
            RoomMessage("정*수", "방금 배정 확정 문자 받았습니다. 마감 전에 다들 서두르세요!", 2200),
            RoomMessage(_MANAGER, "최소 배정 500만 원, 마감까지 세 시간 남았습니다. 회사 수납 계좌 입금 순서대로 확정됩니다.", 2000, True),
            RoomMessage(_MANAGER, "밖으로 알려지면 배정이 취소되니 가족분께도 말씀하지 마세요. 입금 안내는 1:1 상담으로 드립니다.", 2200, True),
        ),
        actions=(
            RoomAction(
                "app_install",
                "전용 거래 앱 받기",
                "출처를 알 수 없는 '전용 거래 앱'은 가짜 투자 앱이나 원격 조종 앱입니다. 설치하는 순간 화면의 수익은 조작되고, 휴대폰 정보가 빠져나갈 수 있습니다.",
            ),
            RoomAction(
                "inquiry",
                "1:1 상담 신청",
                "1:1 상담방에서 입금 계좌를 받는 순간부터 돈을 보내게 됩니다. 원금 보장, 고수익 약속은 그 자체로 불법 투자 권유입니다.",
            ),
            RoomAction(
                "deposit",
                "배정 신청하기 (입금)",
                "회사 수납 계좌로 보낸 돈은 돌려받기 어렵습니다. 공모주 청약은 본인이 쓰는 증권사에서만 할 수 있습니다.",
            ),
        ),
    ),
}


def room_for(scenario_id: str) -> LeadingRoom | None:
    """시나리오에 이어지는 리딩방. 없으면 None이고, 그 통화에는 문자를 보내지 않는다."""
    return LEADING_ROOMS.get(canonical_id(scenario_id))
