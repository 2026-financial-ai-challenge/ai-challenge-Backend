import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models.participant import Participant
from app.periodic import PeriodicWorker, env_int


logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_POLL_SEC = 6 * 60 * 60


def purge_expired_participants(*, now: datetime | None = None) -> int:
    """보존 기간이 지난 참가자를 삭제한다. 훈련 기록은 FK(ON DELETE SET NULL)로 식별 정보 없이 남는다."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=_retention_days())

    with SessionLocal.begin() as db:
        expired_ids = db.scalars(
            select(Participant.id).where(Participant.created_at <= cutoff)
        ).all()
        if not expired_ids:
            return 0
        db.execute(delete(Participant).where(Participant.id.in_(expired_ids)))

    logger.info(
        "Purged %s participant(s) past the %s-day retention period",
        len(expired_ids),
        _retention_days(),
    )
    return len(expired_ids)


def _retention_days() -> int:
    return env_int("PARTICIPANT_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, minimum=1)


retention_worker = PeriodicWorker(
    "participant-data-retention",
    purge_expired_participants,
    lambda: env_int("PARTICIPANT_RETENTION_POLL_SEC", DEFAULT_POLL_SEC, minimum=60),
)
