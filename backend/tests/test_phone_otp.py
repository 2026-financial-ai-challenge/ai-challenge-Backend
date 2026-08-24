from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import otp_service
from app.services.otp_service import reset_otp_state
from app.services.session_service import reset_sessions


PHONE = "01012345678"
OTHER_PHONE = "01087654321"


@pytest.fixture
def client():
    reset_sessions()
    reset_otp_state()
    return TestClient(app)


@pytest.fixture(autouse=True)
def octomo_received(monkeypatch):
    monkeypatch.setattr(otp_service, "message_exists", lambda *args, **kwargs: True)


@pytest.fixture
def clock(monkeypatch):
    current = {"now": datetime(2026, 8, 24, 2, 0, tzinfo=timezone.utc)}

    def fake_now() -> datetime:
        return current["now"]

    monkeypatch.setattr(otp_service, "_now", fake_now)

    def advance(seconds: int) -> None:
        current["now"] = current["now"] + timedelta(seconds=seconds)

    return advance


@pytest.fixture
def started_calls(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_start(session_id: str, phone_number: str) -> None:
        calls.append((session_id, phone_number))

    monkeypatch.setattr(otp_service, "start_training_calls", fake_start)
    return calls


def create_session(client: TestClient) -> str:
    response = client.post(
        "/v1/consents",
        json={"privacy": True, "unannouncedTraining": True},
    )
    assert response.status_code == 200
    return response.json()["sessionId"]


def request_code(client: TestClient, session_id: str, phone: str = PHONE) -> str:
    response = client.post(
        f"/v1/sessions/{session_id}/phone/otp",
        json={"phoneNumber": phone},
    )
    assert response.status_code == 200
    return response.json()["code"]


def test_otp_request_returns_code_but_does_not_confirm_phone(client):
    session_id = create_session(client)

    response = client.post(
        f"/v1/sessions/{session_id}/phone/otp",
        json={"phoneNumber": PHONE},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["phoneNumberMasked"] == "010-****-5678"
    assert body["sendToNumber"] == "16663538"
    assert body["expiresInSec"] == 300
    assert body["resendAvailableInSec"] == 60
    assert body["code"].isdigit() and len(body["code"]) == 6

    session = client.get(f"/v1/sessions/{session_id}").json()["session"]
    assert session["phoneNumberMasked"] is None
    assert session["callStatus"] is None


def test_verify_confirms_phone_and_starts_call(client, started_calls):
    session_id = create_session(client)
    code = request_code(client, session_id)

    response = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )

    assert response.status_code == 200
    session = response.json()["session"]
    assert session["phoneNumberMasked"] == "010-****-5678"
    assert session["callStatus"] == "waiting"
    assert started_calls == [(session_id, PHONE)]


def test_verify_waits_until_octomo_sees_message(client, monkeypatch, started_calls):
    session_id = create_session(client)
    code = request_code(client, session_id)
    monkeypatch.setattr(otp_service, "message_exists", lambda *args, **kwargs: False)

    missing = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )
    assert missing.status_code == 400
    assert missing.json()["code"] == "OTP_NOT_RECEIVED"
    assert started_calls == []

    monkeypatch.setattr(otp_service, "message_exists", lambda *args, **kwargs: True)
    success = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )
    assert success.status_code == 200
    assert started_calls == [(session_id, PHONE)]


def test_old_phone_register_endpoint_is_removed(client):
    session_id = create_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/phone",
        json={"phoneNumber": PHONE},
    )
    assert response.status_code == 404


def test_invalid_phone(client):
    session_id = create_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/phone/otp",
        json={"phoneNumber": "0212345678"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_PHONE"


def test_session_not_found(client):
    response = client.post(
        "/v1/sessions/ses_missing/phone/otp",
        json={"phoneNumber": PHONE},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"


def test_otp_not_requested(client):
    session_id = create_session(client)
    response = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": "123456"},
    )
    assert response.status_code == 400
    assert response.json() == {
        "message": "인증번호를 먼저 요청해 주세요.",
        "code": "OTP_NOT_REQUESTED",
    }


def test_otp_phone_mismatch(client):
    session_id = create_session(client)
    code = request_code(client, session_id)

    response = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": OTHER_PHONE, "code": code},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "OTP_PHONE_MISMATCH"


def test_otp_invalid_then_lock(client):
    session_id = create_session(client)
    code = request_code(client, session_id)

    for remaining in (4, 3, 2, 1):
        response = client.post(
            f"/v1/sessions/{session_id}/phone/verify",
            json={"phoneNumber": PHONE, "code": "000000"},
        )
        assert response.status_code == 400
        assert response.json() == {
            "message": f"인증번호가 올바르지 않습니다. ({remaining}회 남음)",
            "code": "OTP_INVALID",
        }

    locked = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": "000000"},
    )
    assert locked.status_code == 429
    assert locked.json()["code"] == "OTP_LOCKED"

    still_locked = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )
    assert still_locked.status_code == 429
    assert still_locked.json()["code"] == "OTP_LOCKED"


def test_resend_resets_fail_count(client, clock, started_calls):
    session_id = create_session(client)
    request_code(client, session_id)
    client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": "000000"},
    )

    clock(60)
    code = request_code(client, session_id)

    response = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": "000000"},
    )
    assert response.status_code == 400
    assert response.json()["message"] == "인증번호가 올바르지 않습니다. (4회 남음)"

    success = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )
    assert success.status_code == 200
    assert started_calls == [(session_id, PHONE)]


def test_otp_cooldown(client):
    session_id = create_session(client)
    first = client.post(f"/v1/sessions/{session_id}/phone/otp", json={"phoneNumber": PHONE})
    assert first.status_code == 200

    second = client.post(f"/v1/sessions/{session_id}/phone/otp", json={"phoneNumber": PHONE})
    assert second.status_code == 429
    assert second.json()["code"] == "OTP_COOLDOWN"


def test_otp_rate_limited_after_five_sends(client, clock):
    session_id = create_session(client)
    for _ in range(5):
        response = client.post(
            f"/v1/sessions/{session_id}/phone/otp",
            json={"phoneNumber": PHONE},
        )
        assert response.status_code == 200
        clock(60)

    limited = client.post(f"/v1/sessions/{session_id}/phone/otp", json={"phoneNumber": PHONE})
    assert limited.status_code == 429
    assert limited.json()["code"] == "OTP_RATE_LIMITED"


def test_otp_expired(client, clock):
    session_id = create_session(client)
    code = request_code(client, session_id)
    clock(300)

    response = client.post(
        f"/v1/sessions/{session_id}/phone/verify",
        json={"phoneNumber": PHONE, "code": code},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "OTP_EXPIRED"
    session = client.get(f"/v1/sessions/{session_id}").json()["session"]
    assert session["phoneNumberMasked"] is None
    assert session["callStatus"] is None


def test_ip_send_rate_limit(client, monkeypatch):
    monkeypatch.setattr(otp_service, "OTP_SEND_LIMIT_PER_IP_HOUR", 2)
    first = create_session(client)
    second = create_session(client)
    third = create_session(client)

    assert client.post(f"/v1/sessions/{first}/phone/otp", json={"phoneNumber": "01011112222"}).status_code == 200
    assert client.post(f"/v1/sessions/{second}/phone/otp", json={"phoneNumber": "01033334444"}).status_code == 200
    limited = client.post(f"/v1/sessions/{third}/phone/otp", json={"phoneNumber": "01055556666"})
    assert limited.status_code == 429
    assert limited.json()["code"] == "OTP_RATE_LIMITED"


def test_does_not_start_call_before_verify(client, started_calls):
    session_id = create_session(client)
    request_code(client, session_id)
    assert started_calls == []
