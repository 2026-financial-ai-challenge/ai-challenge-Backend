import asyncio
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.errors import ApiError
from app.models.web_training import (
    WEB_EVENT_TYPES,
    WEB_RISK_EVENT_TYPES,
    WebTrainingEvent,
    WebTrainingLink,
)
from app.services.session_service import get_phone_number
from app.services.sms_service import send_sms


logger = logging.getLogger(__name__)

LINK_TTL = timedelta(hours=24)
_DEFAULT_WEB_BASE = "https://gaoncs.vercel.app"
# 사건 조회 포털 링크를 대사에 쓰는 수사기관 사칭 시나리오에만 문자를 보낸다.
WEB_TRAINING_SCENARIO_ID = "investigation_unit"


def create_link(session_id: str) -> str:
    """세션에 대한 1회성 훈련 링크 토큰을 만들고 저장한다."""
    token = secrets.token_urlsafe(24)
    with SessionLocal.begin() as db:
        db.add(
            WebTrainingLink(
                token=token,
                session_id=session_id,
                expires_at=datetime.now(timezone.utc) + LINK_TTL,
            )
        )
    return token


def link_exists(session_id: str) -> bool:
    with SessionLocal() as db:
        return (
            db.scalar(
                select(WebTrainingLink.token)
                .where(WebTrainingLink.session_id == session_id)
                .limit(1)
            )
            is not None
        )


async def dispatch_training_link(session_id: str, scenario_id: str | None) -> None:
    """훈련용 링크를 문자로 보낸다. 세션당 한 번만 보내고, 보낼 수 없는 경우는 조용히 건너뛴다."""
    if scenario_id != WEB_TRAINING_SCENARIO_ID:
        return
    phone = get_phone_number(session_id)
    if not phone:
        return
    if link_exists(session_id):
        return

    token = create_link(session_id)
    base = os.getenv("WEB_TRAINING_BASE_URL", _DEFAULT_WEB_BASE).strip().rstrip("/")
    url = f"{base}/t/{token}"
    body = f"[가온형사사법지원포털] 등기송달 열람 안내입니다. 본인 확인 후 열람하세요. {url}"
    try:
        await asyncio.to_thread(send_sms, phone, body)
    except Exception:
        logger.exception("Web training SMS dispatch failed: session_id=%s", session_id)


def _resolve_active_link(db, token: str) -> WebTrainingLink:
    link = db.get(WebTrainingLink, token)
    if link is None:
        raise ApiError(404, "WEB_LINK_NOT_FOUND", "유효하지 않은 링크입니다.")
    expires_at = link.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise ApiError(410, "WEB_LINK_EXPIRED", "만료된 링크입니다.")
    return link


def verify_link(token: str) -> None:
    """링크 진입 시 유효한지만 확인한다. 위험 행동으로 닫힌 링크는 다른 기기에서도 다시 열 수 없다."""
    with SessionLocal() as db:
        link = _resolve_active_link(db, token)
        if link.closed_at is not None:
            raise ApiError(410, "WEB_LINK_CLOSED", "이미 종료된 훈련입니다.")


def record_event(token: str, event_type: str) -> None:
    """웹 행동 1건을 기록한다. 위험 행동이면 링크를 닫고, 같은 행동의 재전송은 무시한다.

    닫힌 뒤에도 이미 열린 페이지의 이벤트는 받는다. 막는 것은 재진입(verify_link)뿐이다.
    """
    if event_type not in WEB_EVENT_TYPES:
        raise ApiError(400, "WEB_EVENT_INVALID", "알 수 없는 이벤트입니다.")
    with SessionLocal() as db:
        link = _resolve_active_link(db, token)
        if event_type in WEB_RISK_EVENT_TYPES and link.closed_at is None:
            link.closed_at = datetime.now(timezone.utc)
            # 아래 중복 이벤트 롤백이 링크 닫힘까지 되돌리지 않도록 먼저 커밋한다.
            db.commit()
        db.add(
            WebTrainingEvent(
                token=token,
                session_id=link.session_id,
                event_type=event_type,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()

