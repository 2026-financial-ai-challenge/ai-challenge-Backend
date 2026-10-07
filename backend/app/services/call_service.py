import asyncio
import logging
import os
from datetime import datetime, timezone
from threading import Lock, Thread

from sqlalchemy import select

from app.database import SessionLocal
from app.models.call import Call
from app.services import report_service
from app.services.report_service import (
    bind_call,
    request_clawops_transcript,
    session_id_for_call,
)
from app.services.session_service import (
    attach_call,
    get_phone_number,
    get_session,
    mask_phone_number,
    update_call_status,
    update_report_status,
)
from app.training.scenarios import ensure_ai_importable, get_runtime_scenario


logger = logging.getLogger(__name__)

# ClawOps 콜백 "이벤트" 이름이다(통화 상태 이름이 아님).
_STATUS_CALLBACK_EVENTS = "ringing answered completed"
_MISSED_STATUSES = {"no-answer", "busy", "rejected", "canceled"}
_RINGING_STATUSES = {"queued", "ringing", "in-progress"}

_outbound_lock = Lock()


class CallServiceError(Exception):
    pass


def start_training_calls(session_id: str, phone_number: str) -> None:
    """HTTP 요청을 붙잡지 않도록 발신은 백그라운드 스레드에서 한다."""
    logger.info(
        "Starting training call session=%s phone=%s",
        session_id,
        mask_phone_number(phone_number),
    )
    update_call_status(session_id, "waiting")
    Thread(target=_run_training_call, args=(session_id,), daemon=True).start()


def _run_training_call(session_id: str) -> None:
    if not _outbound_lock.acquire(timeout=20):
        update_call_status(session_id, "failed")
        _retry_scheduled_training(session_id, "outbound_lock_timeout")
        logger.warning("Previous outbound call still starting; skip session=%s", session_id)
        return
    try:
        asyncio.run(_start_call(session_id))
    except Exception:
        update_call_status(session_id, "failed")
        _retry_scheduled_training(session_id, "call_start_failed")
        logger.exception("Failed to start call: session_id=%s", session_id)
    finally:
        _outbound_lock.release()


async def _start_call(session_id: str) -> None:
    """ClawOps 매니지드 에이전트로 발신한다. 이후 진행은 상태·녹취록 웹훅이 맡는다."""
    session = get_session(session_id)
    if session is None:
        raise CallServiceError(f"session not found: {session_id}")
    if session.callStatus == "calling":
        raise CallServiceError(f"call already in progress: {session_id}")
    phone_number = get_phone_number(session_id)
    if phone_number is None:
        raise CallServiceError(f"no phone number: {session_id}")

    _require_env("CLAWOPS_API_KEY")
    _require_env("CLAWOPS_ACCOUNT_ID")
    from_number = _outbound_phone_number(session.currentTrainingType)
    scenario = get_runtime_scenario(session.currentTrainingType)

    ensure_ai_importable()
    from ai.managed_agent import build_call_context, pick_variant, resolve_agent_id

    variant = pick_variant()
    agent_id = await asyncio.to_thread(resolve_agent_id, scenario.id, variant)
    request = {
        "to": phone_number,
        "from_": from_number,
        "agent_id": agent_id,
        "call_context": build_call_context(scenario),
        "timeout": 30,
    }
    callback = _status_callback_url()
    if callback:
        request["status_callback"] = callback
        request["status_callback_event"] = _STATUS_CALLBACK_EVENTS

    calls = report_service._clawops_calls()
    call = await asyncio.to_thread(lambda: calls.create(**request))
    bind_call(session_id, call.call_id)
    attach_call(session_id, call.call_id, scenario_id=scenario.id, agent_variant=variant)
    logger.info(
        "Started call session=%s call_id=%s scenario=%s variant=%s",
        session_id,
        call.call_id,
        scenario.id,
        variant,
    )


def _status_callback_url() -> str:
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        logger.warning("PUBLIC_BASE_URL is not set; ClawOps cannot report call status")
        return ""
    return f"{base}/v1/webhooks/clawops/status"


async def handle_call_status_event(call_id: str) -> None:
    """상태 웹훅 처리. 웹훅 본문 형식은 문서화돼 있지 않아 상태는 API로 다시 조회한다."""
    session_id = session_id_for_call(call_id)
    if session_id is None:
        logger.info("No training session for ClawOps call_id=%s", call_id)
        return

    call = await asyncio.to_thread(lambda: report_service._clawops_calls().get(call_id))
    status_name = (getattr(call, "status", "") or "").strip()

    if status_name in _RINGING_STATUSES:
        update_call_status(session_id, "calling")
        return

    if status_name == "completed":
        update_call_status(session_id, "completed")
        _complete_call(call_id)
        _complete_scheduled_training(session_id)
        try:
            from app.services.training_scheduler import schedule_unannounced_training

            schedule_unannounced_training(session_id)
        except Exception:
            logger.exception("Unannounced training scheduling failed: session_id=%s", session_id)
        try:
            await request_clawops_transcript(call_id)
        except Exception:
            logger.exception("ClawOps transcript request failed: session_id=%s", session_id)
        return

    if status_name in _MISSED_STATUSES:
        update_call_status(session_id, "missed")
        _fail_call(call_id, status_name)
        update_report_status(session_id, "none")
        _retry_scheduled_training(session_id, f"call_{status_name}")
        logger.info("Training call missed session=%s status=%s", session_id, status_name)
        return

    cause = getattr(call, "hangup_cause", None)
    update_call_status(session_id, "failed")
    _fail_call(call_id, cause or status_name or "unknown")
    update_report_status(session_id, "none")
    _retry_scheduled_training(session_id, "call_failed")
    logger.warning(
        "Training call failed session=%s status=%s cause=%s", session_id, status_name, cause
    )


def _complete_call(clawops_call_id: str) -> None:
    _finish_call(clawops_call_id, "completed")


def _fail_call(clawops_call_id: str, reason: str) -> None:
    _finish_call(clawops_call_id, "failed", reason)


def _finish_call(clawops_call_id: str, status: str, reason: str | None = None) -> None:
    with SessionLocal.begin() as db:
        call = db.scalar(select(Call).where(Call.clawops_call_id == clawops_call_id))
        if call is None:
            return
        call.status = status
        call.completed_at = datetime.now(timezone.utc)
        if reason is not None:
            call.failure_reason = reason


def _outbound_phone_number(training_type: str) -> str:
    if training_type == "unannounced":
        return _require_env("CLAWOPS_UNANNOUNCED_PHONE_NUMBER")
    return _require_env("CLAWOPS_PHONE_NUMBER")


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CallServiceError(f"{name} is not set")
    # 복사해 붙인 키에 섞여 들어온 공백·비ASCII 문자는 인증 실패를 일으켜 걸러 낸다.
    cleaned = "".join(ch for ch in value if ch.isascii() and not ch.isspace())
    if cleaned != value:
        logger.warning("%s had non-ASCII or whitespace characters; they were stripped", name)
        os.environ[name] = cleaned
    return cleaned


def _complete_scheduled_training(session_id: str) -> None:
    from app.services.training_scheduler import complete_unannounced_training

    complete_unannounced_training(session_id)


def _retry_scheduled_training(session_id: str, reason: str) -> None:
    from app.services.training_scheduler import retry_unannounced_training

    retry_unannounced_training(session_id, reason)
