from datetime import datetime, timezone
from threading import Lock
from uuid import uuid4

from app.schemas.consent import ConsentRecord, SessionResponse


_sessions: dict[str, SessionResponse] = {}
_confirmed_phones: dict[str, str] = {}
_sessions_lock = Lock()


def create_session(privacy: bool, unannounced_training: bool) -> SessionResponse:
    now = datetime.now(timezone.utc)
    session = SessionResponse(
        id=f"ses_{uuid4().hex}",
        phoneNumberMasked=None,
        callStatus=None,
        currentTrainingType="announced",
        consents=ConsentRecord(
            privacy=privacy,
            unannouncedTraining=unannounced_training,
            consentedAt=now,
        ),
        createdAt=now,
        updatedAt=now,
    )

    with _sessions_lock:
        _sessions[session.id] = session

    return session


def get_session(session_id: str) -> SessionResponse | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None
        return session.model_copy(deep=True)


def session_exists(session_id: str) -> bool:
    with _sessions_lock:
        return session_id in _sessions


def confirm_verified_phone(session_id: str, phone_number: str) -> SessionResponse | None:
    with _sessions_lock:
        session = _sessions.get(session_id)
        if session is None:
            return None

        session.phoneNumberMasked = mask_phone_number(phone_number)
        session.callStatus = "waiting"
        session.updatedAt = datetime.now(timezone.utc)
        _confirmed_phones[session_id] = phone_number
        return session.model_copy(deep=True)


def mask_phone_number(phone_number: str) -> str:
    if len(phone_number) == 11:
        return f"{phone_number[:3]}-****-{phone_number[7:]}"
    return f"{phone_number[:3]}-***-{phone_number[6:]}"


def reset_sessions() -> None:
    with _sessions_lock:
        _sessions.clear()
        _confirmed_phones.clear()
