from datetime import datetime, timedelta, timezone
from itertools import count

from app.database import SessionLocal
from app.models.participant import Participant
from app.models.training_session import TrainingSession
from app.services.data_retention import purge_expired_participants
from app.services.session_service import create_session, reset_sessions

_phone_numbers = count(1)


def setup_function() -> None:
    reset_sessions()


def _participant(created_at: datetime) -> int:
    with SessionLocal.begin() as db:
        participant = Participant(
            phone_number=f"0109999{next(_phone_numbers):04d}",
            password_hash="not-a-real-hash",
            created_at=created_at,
        )
        db.add(participant)
        db.flush()
        return participant.id


def test_purges_participants_past_the_retention_period():
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    expired_id = _participant(now - timedelta(days=31))
    fresh_id = _participant(now - timedelta(days=1))

    purged = purge_expired_participants(now=now)

    assert purged == 1
    with SessionLocal() as db:
        assert db.get(Participant, expired_id) is None
        assert db.get(Participant, fresh_id) is not None


def test_purge_is_a_no_op_when_nothing_is_expired():
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    _participant(now - timedelta(days=1))

    assert purge_expired_participants(now=now) == 0


def test_purging_a_participant_keeps_the_training_session_anonymized():
    now = datetime(2026, 9, 7, tzinfo=timezone.utc)
    expired_id = _participant(now - timedelta(days=31))
    session = create_session(
        privacy=True,
        unannounced_training=False,
        participant_id=expired_id,
    )

    purge_expired_participants(now=now)

    with SessionLocal() as db:
        persisted = db.get(TrainingSession, session.id)
        assert persisted is not None
        assert persisted.participant_id is None
