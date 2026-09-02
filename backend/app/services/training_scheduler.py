import logging
import os
from datetime import datetime, timedelta, timezone
from secrets import randbelow
from threading import Event, Lock, Thread
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import SessionLocal
from app.models.consent import Consent
from app.models.scheduled_training import ScheduledTraining
from app.models.training_session import TrainingSession


logger = logging.getLogger(__name__)
DEFAULT_MIN_DELAY_SEC = 30 * 60
DEFAULT_MAX_DELAY_SEC = 60 * 60
SCHEDULER_POLL_SEC = 30
DEFAULT_RETRY_DELAY_SEC = 5 * 60
DEFAULT_MAX_ATTEMPTS = 2
DISPATCH_STALE_SEC = 10 * 60

_scheduler_lock = Lock()
_scheduler_stop = Event()
_scheduler_thread: Thread | None = None


def schedule_unannounced_training(
    source_session_id: str,
    *,
    now: datetime | None = None,
    delay_seconds: int | None = None,
) -> ScheduledTraining | None:
    now = now or datetime.now(timezone.utc)
    minimum, maximum = _delay_range()
    delay = (
        delay_seconds
        if delay_seconds is not None
        else minimum + randbelow(maximum - minimum + 1)
    )
    if delay < minimum or delay > maximum:
        raise ValueError("unannounced training delay is outside the configured range")

    with SessionLocal.begin() as db:
        existing = db.scalar(
            select(ScheduledTraining).where(
                ScheduledTraining.source_session_id == source_session_id
            )
        )
        if existing is not None:
            return existing

        source = db.scalar(
            select(TrainingSession)
            .options(selectinload(TrainingSession.consent))
            .where(TrainingSession.id == source_session_id)
        )
        if (
            source is None
            or source.current_training_type != "announced"
            or source.participant_id is None
            or source.consent is None
            or not source.consent.surprise_call_agreed
        ):
            return None

        scheduled = ScheduledTraining(
            source_session_id=source_session_id,
            training_type="unannounced",
            status="pending",
            scheduled_at=now + timedelta(seconds=delay),
            created_at=now,
        )
        db.add(scheduled)
        db.flush()
        logger.info(
            "Unannounced training scheduled source_session=%s scheduled_at=%s",
            source_session_id,
            scheduled.scheduled_at.isoformat(),
        )
        return scheduled


def process_due_scheduled_trainings(*, now: datetime | None = None) -> int:
    if not os.getenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "").strip():
        logger.debug("CLAWOPS_UNANNOUNCED_PHONE_NUMBER is not set; scheduled calls wait")
        return 0

    now = now or datetime.now(timezone.utc)
    started: list[tuple[str, str]] = []
    with SessionLocal.begin() as db:
        stale_jobs = db.scalars(
            select(ScheduledTraining)
            .where(
                ScheduledTraining.status == "started",
                ScheduledTraining.started_at
                <= now - timedelta(seconds=DISPATCH_STALE_SEC),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for job in stale_jobs:
            _retry_or_fail(job, now=now, reason="dispatch_timeout")

        jobs = db.scalars(
            select(ScheduledTraining)
            .where(
                ScheduledTraining.status == "pending",
                ScheduledTraining.scheduled_at <= now,
            )
            .order_by(ScheduledTraining.scheduled_at)
            .with_for_update(skip_locked=True)
        ).all()
        for job in jobs:
            source = db.scalar(
                select(TrainingSession)
                .options(
                    selectinload(TrainingSession.consent),
                    selectinload(TrainingSession.participant),
                )
                .where(TrainingSession.id == job.source_session_id)
            )
            if source is None or source.participant is None or source.consent is None:
                job.status = "cancelled"
                continue

            result = (
                db.get(TrainingSession, job.result_session_id)
                if job.result_session_id
                else None
            )
            if result is None:
                result = TrainingSession(
                    id=f"ses_{uuid4().hex}",
                    participant_id=source.participant_id,
                    current_training_type="unannounced",
                    call_status="waiting",
                    created_at=now,
                    updated_at=now,
                )
                result.consent = Consent(
                    privacy_agreed=source.consent.privacy_agreed,
                    surprise_call_agreed=source.consent.surprise_call_agreed,
                    consented_at=source.consent.consented_at,
                )
                db.add(result)
                db.flush()
                job.result_session_id = result.id
            job.status = "started"
            job.started_at = now
            job.attempt_count += 1
            job.last_error = None
            started.append((result.id, source.participant.phone_number))

    if started:
        from app.services.call_service import start_training_calls

        for session_id, phone_number in started:
            start_training_calls(session_id, phone_number)
            logger.info("Unannounced training started session=%s", session_id)
    return len(started)


def complete_unannounced_training(
    result_session_id: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(ScheduledTraining)
            .where(ScheduledTraining.result_session_id == result_session_id)
            .with_for_update()
        )
        if job is None or job.status in {"completed", "cancelled"}:
            return
        job.status = "completed"
        job.completed_at = now
        job.last_error = None
        logger.info(
            "Unannounced training completed session=%s attempts=%s",
            result_session_id,
            job.attempt_count,
        )


def retry_unannounced_training(
    result_session_id: str,
    reason: str,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    with SessionLocal.begin() as db:
        job = db.scalar(
            select(ScheduledTraining)
            .where(ScheduledTraining.result_session_id == result_session_id)
            .with_for_update()
        )
        if job is None or job.status in {"completed", "failed", "cancelled"}:
            return
        _retry_or_fail(job, now=now, reason=reason)


def _retry_or_fail(
    job: ScheduledTraining,
    *,
    now: datetime,
    reason: str,
) -> None:
    job.last_error = reason[:500]
    job.started_at = None
    if job.attempt_count >= _max_attempts():
        job.status = "failed"
        logger.error(
            "Unannounced training exhausted retries session=%s attempts=%s reason=%s",
            job.result_session_id,
            job.attempt_count,
            reason,
        )
        return
    job.status = "pending"
    job.scheduled_at = now + timedelta(seconds=_retry_delay_sec())
    logger.warning(
        "Unannounced training rescheduled session=%s attempt=%s scheduled_at=%s reason=%s",
        job.result_session_id,
        job.attempt_count,
        job.scheduled_at.isoformat(),
        reason,
    )


def start_training_scheduler() -> None:
    global _scheduler_thread
    with _scheduler_lock:
        if _scheduler_thread is not None and _scheduler_thread.is_alive():
            return
        _scheduler_stop.clear()
        _scheduler_thread = Thread(
            target=_scheduler_loop,
            name="unannounced-training-scheduler",
            daemon=True,
        )
        _scheduler_thread.start()


def stop_training_scheduler() -> None:
    _scheduler_stop.set()


def _scheduler_loop() -> None:
    while not _scheduler_stop.is_set():
        try:
            process_due_scheduled_trainings()
        except Exception:
            logger.exception("Scheduled training poll failed")
        _scheduler_stop.wait(SCHEDULER_POLL_SEC)


def _delay_range() -> tuple[int, int]:
    minimum = int(
        os.getenv("UNANNOUNCED_CALL_MIN_DELAY_SEC", str(DEFAULT_MIN_DELAY_SEC))
    )
    maximum = int(
        os.getenv("UNANNOUNCED_CALL_MAX_DELAY_SEC", str(DEFAULT_MAX_DELAY_SEC))
    )
    if minimum < 0 or maximum < minimum:
        raise RuntimeError("Invalid unannounced call delay range")
    return minimum, maximum


def _retry_delay_sec() -> int:
    return max(
        0,
        int(
            os.getenv(
                "UNANNOUNCED_CALL_RETRY_DELAY_SEC",
                str(DEFAULT_RETRY_DELAY_SEC),
            )
        ),
    )


def _max_attempts() -> int:
    return max(
        1,
        int(
            os.getenv(
                "UNANNOUNCED_CALL_MAX_ATTEMPTS",
                str(DEFAULT_MAX_ATTEMPTS),
            )
        ),
    )
