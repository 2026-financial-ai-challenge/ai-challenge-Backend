from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models.call import Call
from app.models.consent import Consent
from app.models.participant import Participant
from app.models.phone_verification import PhoneVerification
from app.models.training_session import TrainingSession
from app.models.transcript_event import TranscriptEvent
from app.schemas.consent import ConsentRecord, SessionResponse


def create_session(
    privacy: bool,
    unannounced_training: bool,
    participant_id: int | None = None,
) -> SessionResponse:
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        session = TrainingSession(
            id=f"ses_{uuid4().hex}",
            current_training_type="announced",
            participant_id=participant_id,
            call_status="waiting" if participant_id is not None else None,
            created_at=now,
            updated_at=now,
        )
        session.consent = Consent(
            privacy_agreed=privacy,
            surprise_call_agreed=unannounced_training,
            consented_at=now,
        )
        db.add(session)
        db.flush()
        return _to_response(session)


def get_session(session_id: str) -> SessionResponse | None:
    with SessionLocal() as db:
        session = db.scalar(_session_query(session_id))
        return _to_response(session) if session is not None else None


def session_exists(session_id: str) -> bool:
    with SessionLocal() as db:
        return db.get(TrainingSession, session_id) is not None


def get_phone_number(session_id: str) -> str | None:
    with SessionLocal() as db:
        return db.scalar(
            select(Participant.phone_number)
            .join(TrainingSession, TrainingSession.participant_id == Participant.id)
            .where(TrainingSession.id == session_id)
        )


def update_call_status(
    session_id: str,
    call_status: Literal["waiting", "calling", "completed"],
) -> SessionResponse | None:
    with SessionLocal.begin() as db:
        session = db.scalar(_session_query(session_id))
        if session is None:
            return None

        session.call_status = call_status
        session.updated_at = datetime.now(timezone.utc)
        db.flush()
        return _to_response(session)


def attach_call(session_id: str, call_id: str) -> SessionResponse | None:
    with SessionLocal.begin() as db:
        session = db.scalar(_session_query(session_id))
        if session is None:
            return None

        call = db.scalar(select(Call).where(Call.clawops_call_id == call_id))
        if call is None:
            db.add(Call(session_id=session_id, clawops_call_id=call_id, status="calling"))
        else:
            call.session_id = session_id
            call.status = "calling"
        session.call_status = "calling"
        if session.report_status is None:
            session.report_status = "pending"
        session.updated_at = datetime.now(timezone.utc)
        db.flush()
        return _to_response(session)


def set_session_call_id(session_id: str, call_id: str) -> SessionResponse | None:
    with SessionLocal.begin() as db:
        session = db.scalar(_session_query(session_id))
        if session is None:
            return None

        call = db.scalar(select(Call).where(Call.clawops_call_id == call_id))
        if call is None:
            db.add(Call(session_id=session_id, clawops_call_id=call_id, status="calling"))
        session.updated_at = datetime.now(timezone.utc)
        db.flush()
        return _to_response(session)


def update_report_status(
    session_id: str,
    report_status: Literal["none", "pending", "draft", "final", "failed"],
) -> SessionResponse | None:
    with SessionLocal.begin() as db:
        session = db.scalar(_session_query(session_id))
        if session is None:
            return None

        session.report_status = report_status
        session.updated_at = datetime.now(timezone.utc)
        db.flush()
        return _to_response(session)


def mask_phone_number(phone_number: str) -> str:
    if len(phone_number) == 11:
        return f"{phone_number[:3]}-****-{phone_number[7:]}"
    return f"{phone_number[:3]}-***-{phone_number[6:]}"


def reset_sessions() -> None:
    with SessionLocal.begin() as db:
        db.execute(delete(TranscriptEvent))
        db.execute(delete(Call))
        db.execute(delete(Consent))
        db.execute(delete(TrainingSession))
        db.execute(delete(PhoneVerification))
        db.execute(delete(Participant))
    from app.services.report_service import reset_reports

    reset_reports()


def _session_query(session_id: str):
    return (
        select(TrainingSession)
        .options(
            selectinload(TrainingSession.consent),
            selectinload(TrainingSession.participant),
            selectinload(TrainingSession.calls),
        )
        .where(TrainingSession.id == session_id)
    )


def _to_response(session: TrainingSession) -> SessionResponse:
    consent = session.consent
    latest_call = max(session.calls, key=lambda call: call.created_at, default=None)
    return SessionResponse(
        id=session.id,
        phoneNumberMasked=(
            mask_phone_number(session.participant.phone_number)
            if session.participant is not None
            else None
        ),
        callStatus=session.call_status,
        callId=latest_call.clawops_call_id if latest_call is not None else None,
        reportStatus=session.report_status,
        currentTrainingType=session.current_training_type,
        consents=ConsentRecord(
            privacy=consent.privacy_agreed,
            unannouncedTraining=consent.surprise_call_agreed,
            consentedAt=consent.consented_at,
        ),
        createdAt=session.created_at,
        updatedAt=session.updated_at,
    )
