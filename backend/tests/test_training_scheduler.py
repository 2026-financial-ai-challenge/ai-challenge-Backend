from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.database import SessionLocal
from app.models.participant import Participant
from app.models.scheduled_training import ScheduledTraining
from app.models.training_session import TrainingSession
from app.services import call_service
from app.services.auth_service import hash_password
from app.services.session_service import create_session, reset_sessions
from app.services.training_scheduler import (
    process_due_scheduled_trainings,
    schedule_unannounced_training,
)


def setup_function() -> None:
    reset_sessions()


def _announced_session() -> str:
    with SessionLocal.begin() as db:
        participant = Participant(
            phone_number="01022223333",
            password_hash=hash_password("testPassword1"),
        )
        db.add(participant)
        db.flush()
        participant_id = participant.id
    return create_session(
        privacy=True,
        unannounced_training=True,
        participant_id=participant_id,
    ).id


def test_schedules_once_between_30_minutes_and_3_hours():
    session_id = _announced_session()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)

    scheduled = schedule_unannounced_training(
        session_id, now=now, delay_seconds=1800
    )
    duplicate = schedule_unannounced_training(
        session_id, now=now, delay_seconds=3600
    )

    assert scheduled is not None
    assert duplicate is not None
    assert duplicate.id == scheduled.id
    assert scheduled.scheduled_at == now + timedelta(minutes=30)
    assert scheduled.status == "pending"


def test_due_training_waits_until_second_caller_number_is_set(monkeypatch):
    session_id = _announced_session()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(session_id, now=now, delay_seconds=1800)
    monkeypatch.delenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", raising=False)

    assert process_due_scheduled_trainings(now=now + timedelta(minutes=31)) == 0
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None and job.status == "pending"


def test_due_training_creates_separate_session_and_starts_call(monkeypatch):
    source_session_id = _announced_session()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(
        source_session_id, now=now, delay_seconds=1800
    )
    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07011112222")
    started: list[tuple[str, str]] = []
    monkeypatch.setattr(
        call_service,
        "start_training_calls",
        lambda session_id, phone: started.append((session_id, phone)),
    )

    count = process_due_scheduled_trainings(now=now + timedelta(minutes=31))

    assert count == 1
    assert len(started) == 1
    result_session_id, phone = started[0]
    assert phone == "01022223333"
    assert result_session_id != source_session_id
    with SessionLocal() as db:
        result = db.get(TrainingSession, result_session_id)
        job = db.scalar(select(ScheduledTraining))
        assert result is not None
        assert result.current_training_type == "unannounced"
        assert result.participant_id is not None
        assert result.consent.privacy_agreed is True
        assert job is not None
        assert job.status == "started"
        assert job.result_session_id == result_session_id


def test_unannounced_call_uses_separate_originating_number(monkeypatch):
    monkeypatch.setenv("CLAWOPS_PHONE_NUMBER", "07033334444")
    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07055556666")

    assert call_service._outbound_phone_number("announced") == "07033334444"
    assert call_service._outbound_phone_number("unannounced") == "07055556666"
