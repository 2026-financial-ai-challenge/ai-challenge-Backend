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
    complete_unannounced_training,
    process_due_scheduled_trainings,
    retry_unannounced_training,
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


def test_schedules_once_between_30_minutes_and_1_hour():
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


def test_failed_unannounced_call_is_retried_once_then_failed(monkeypatch):
    source_session_id = _announced_session()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(source_session_id, now=now, delay_seconds=1800)
    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07011112222")
    monkeypatch.setenv("UNANNOUNCED_CALL_RETRY_DELAY_SEC", "300")
    monkeypatch.setenv("UNANNOUNCED_CALL_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(call_service, "start_training_calls", lambda *_args: None)

    assert process_due_scheduled_trainings(now=now + timedelta(minutes=31)) == 1
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None
        result_session_id = job.result_session_id
        assert result_session_id is not None
        assert job.attempt_count == 1

    retry_unannounced_training(
        result_session_id,
        "call_not_connected",
        now=now + timedelta(minutes=31),
    )
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None
        assert job.status == "pending"
        assert job.last_error == "call_not_connected"

    assert process_due_scheduled_trainings(now=now + timedelta(hours=2)) == 1
    retry_unannounced_training(
        result_session_id,
        "call_not_connected",
        now=now + timedelta(hours=2),
    )
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None
        assert job.status == "failed"
        assert job.attempt_count == 2


def test_completed_unannounced_call_marks_job_completed(monkeypatch):
    source_session_id = _announced_session()
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(source_session_id, now=now, delay_seconds=1800)
    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07011112222")
    monkeypatch.setattr(call_service, "start_training_calls", lambda *_args: None)

    process_due_scheduled_trainings(now=now + timedelta(minutes=31))
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None and job.result_session_id is not None
        result_session_id = job.result_session_id

    complete_unannounced_training(result_session_id, now=now + timedelta(minutes=32))
    with SessionLocal() as db:
        job = db.scalar(select(ScheduledTraining))
        assert job is not None
        assert job.status == "completed"
        assert job.completed_at is not None
