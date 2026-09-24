import logging
import os
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.models.participant import Participant


logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30
DEFAULT_POLL_SEC = 6 * 60 * 60

_retention_lock = Lock()
_retention_stop = Event()
_retention_thread: Thread | None = None


def purge_expired_participants(*, now: datetime | None = None) -> int:
    """가입(created_at) 후 보존기간이 지난 참가자의 개인정보를 파기한다.

    참가자 행을 삭제하면 training_sessions.participant_id는 FK의
    ON DELETE SET NULL로 끊어져, 훈련 기록(세션/전사/리포트)은 개인 식별
    정보 없이 그대로 남는다.
    """
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


def start_retention_scheduler() -> None:
    global _retention_thread
    with _retention_lock:
        if _retention_thread is not None and _retention_thread.is_alive():
            return
        _retention_stop.clear()
        _retention_thread = Thread(
            target=_retention_loop,
            name="participant-data-retention",
            daemon=True,
        )
        _retention_thread.start()


def stop_retention_scheduler() -> None:
    _retention_stop.set()


def _retention_loop() -> None:
    while not _retention_stop.is_set():
        try:
            purge_expired_participants()
        except Exception:
            logger.exception("Participant data retention purge failed")
        _retention_stop.wait(_poll_sec())


def _retention_days() -> int:
    return max(
        1, int(os.getenv("PARTICIPANT_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS)))
    )


def _poll_sec() -> int:
    return max(
        60, int(os.getenv("PARTICIPANT_RETENTION_POLL_SEC", str(DEFAULT_POLL_SEC)))
    )
