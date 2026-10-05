"""리딩방 대본 회귀 테스트."""

from ai.safety import REAL_ORGS, SPOKEN_META, UNSAFE_TOKEN
from ai.scenarios import SCENARIOS
from ai.scenarios.leading_room import LEADING_ROOMS, ROOM_EVENTS, TERMINAL_EVENTS, room_for


def _texts(room):
    yield room.sms_body
    yield room.room_title
    yield room.invite_text
    yield room.input_notice
    for message in room.messages:
        yield message.sender
        yield message.text
    for action in room.actions:
        yield action.label


def test_room_text_passes_safety_gates():
    for room in LEADING_ROOMS.values():
        for text in _texts(room):
            assert not REAL_ORGS.search(text), text
            assert not UNSAFE_TOKEN.search(text), text
            # 경고 문구가 아닌 곳에서 '훈련'이라고 하면 들통난다
            assert not SPOKEN_META.search(text), text
        for action in room.actions:
            assert not REAL_ORGS.search(action.warning), action.warning


def test_rooms_match_scenarios_and_events():
    for scenario_id, room in LEADING_ROOMS.items():
        assert scenario_id in SCENARIOS
        assert room.scenario_id == scenario_id
        assert {a.event for a in room.actions} == set(TERMINAL_EVENTS)
    assert set(TERMINAL_EVENTS) <= set(ROOM_EVENTS)


def test_room_for_resolves_aliases():
    assert room_for("ipo_allocation") is LEADING_ROOMS["ipo_allocation"]
    # 예전 id로 기록된 통화도 같은 방으로 이어진다
    assert room_for("delivery_payment_error") is LEADING_ROOMS["ipo_allocation"]
    assert room_for("card_delivery") is None
    assert room_for("") is None
