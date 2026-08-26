import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import SessionLocal
from app.main import app
from app.models.phone_verification import PhoneVerification
from app.services import auth_service
from app.services.session_service import reset_sessions


PHONE = "01012345678"
PASSWORD = "safePass123"


@pytest.fixture
def client(monkeypatch):
    reset_sessions()
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_service,
        "send_verification_code",
        lambda phone, code: sent.append((phone, code)),
    )
    monkeypatch.setattr(auth_service, "expose_dev_code", lambda: False)
    return TestClient(app), sent


def _verify(client: TestClient, sent: list[tuple[str, str]]) -> str:
    requested = client.post("/v1/auth/signup/otp", json={"phoneNumber": PHONE})
    assert requested.status_code == 200
    assert requested.json()["devCode"] is None
    assert sent and sent[-1][0] == PHONE

    verified = client.post(
        "/v1/auth/signup/verify",
        json={"phoneNumber": PHONE, "code": sent[-1][1]},
    )
    assert verified.status_code == 200
    return verified.json()["verificationToken"]


def test_signup_login_and_authenticated_consent(client, monkeypatch):
    http, sent = client
    token = _verify(http, sent)
    signup = http.post(
        "/v1/auth/signup",
        json={"verificationToken": token, "password": PASSWORD},
    )
    assert signup.status_code == 201
    body = signup.json()
    assert body["tokenType"] == "bearer"
    assert body["participant"]["phoneNumberMasked"] == "010-****-5678"
    assert auth_service.decode_access_token(body["accessToken"]) == body["participant"]["id"]

    login = http.post(
        "/v1/auth/login",
        json={"phoneNumber": PHONE, "password": PASSWORD},
    )
    assert login.status_code == 200
    access_token = login.json()["accessToken"]

    from app.routers import consent

    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        consent,
        "start_training_calls",
        lambda session_id, phone: started.append((session_id, phone)),
    )
    response = http.post(
        "/v1/consents",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"privacy": True, "unannouncedTraining": True},
    )
    assert response.status_code == 200
    assert started == [(response.json()["sessionId"], PHONE)]


def test_verification_token_is_one_time(client):
    http, sent = client
    token = _verify(http, sent)
    first = http.post(
        "/v1/auth/signup",
        json={"verificationToken": token, "password": PASSWORD},
    )
    assert first.status_code == 201
    second = http.post(
        "/v1/auth/signup",
        json={"verificationToken": token, "password": PASSWORD},
    )
    assert second.status_code == 400
    assert second.json()["code"] == "VERIFICATION_TOKEN_USED"


def test_invalid_credentials_and_auth_required(client):
    http, _sent = client
    login = http.post(
        "/v1/auth/login",
        json={"phoneNumber": PHONE, "password": "wrongPassword1"},
    )
    assert login.status_code == 401
    assert login.json()["code"] == "INVALID_CREDENTIALS"

    consent = http.post(
        "/v1/consents",
        json={"privacy": True, "unannouncedTraining": True},
    )
    assert consent.status_code == 401
    assert consent.json()["code"] == "AUTH_REQUIRED"


def test_sms_failure_does_not_persist_challenge(client, monkeypatch):
    http, _sent = client
    monkeypatch.setattr(
        auth_service,
        "send_verification_code",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("delivery failed")),
    )
    response = http.post("/v1/auth/signup/otp", json={"phoneNumber": PHONE})
    assert response.status_code == 502
    assert response.json()["code"] == "SMS_SEND_FAILED"
    with SessionLocal() as db:
        count = db.scalar(select(func.count()).select_from(PhoneVerification))
    assert count == 0
