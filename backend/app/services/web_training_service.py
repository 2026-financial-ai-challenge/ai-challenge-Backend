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
    WebTrainingEvent,
    WebTrainingLink,
)
from app.services.report_service import score_web_events
from app.services.session_service import get_phone_number
from app.services.sms_service import send_sms


logger = logging.getLogger(__name__)

# 링크 유효 기간. 전화 직후 보내는 문자가 하루 안에는 열릴 것이라는 가정.
LINK_TTL = timedelta(hours=24)

# 문자에 담길 훈련 페이지의 기본 주소. 프론트의 /t/{token} 라우트로 연결된다.
_DEFAULT_WEB_BASE = "https://gaoncs.vercel.app"

# 웹 훈련 링크는 가온형사사법지원포털을 대사에 쓰는 '수사기관 사칭' 시나리오
# 뒤에만 의미가 있다. 다른 시나리오로 건 통화에는 문자를 보내지 않는다.
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
    """세션에 이미 발급된 링크가 있는지. 통화 종료 시 중복 발송을 막는 데 쓴다."""
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
    """통화 종료 후 훈련용 스미싱 링크를 문자로 1회 발송한다.

    ``investigation_unit`` 시나리오로 건 통화에만 보낸다. 훈련 흐름에서
    호출되므로 어떤 이유로든(시나리오 불일치·연락처 없음·설정 누락·발송 실패)
    조용히 건너뛴다. 세션당 한 번만 — 웹훅 재전송이나 재시도로 중복 발송되지
    않도록 이미 링크가 있으면 아무 것도 하지 않는다.
    """
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
        # ClawOps SDK는 동기 호출이라 이벤트 루프를 막지 않도록 스레드로 보낸다.
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
    """``/t/{token}`` 진입 시 링크가 살아 있는지만 확인한다 (행동 기록 없음)."""
    with SessionLocal() as db:
        _resolve_active_link(db, token)


def record_event(token: str, event_type: str) -> None:
    """훈련자의 웹 행동 1건을 기록한다. 같은 행동 재전송은 조용히 무시한다."""
    if event_type not in WEB_EVENT_TYPES:
        raise ApiError(400, "WEB_EVENT_INVALID", "알 수 없는 이벤트입니다.")
    with SessionLocal() as db:
        link = _resolve_active_link(db, token)
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
            # (token, event_type) 유니크 제약 — 같은 행동을 다시 누른 경우.
            db.rollback()


def get_web_report(session_id: str):
    """세션의 웹 훈련 결과를 채점해 반환한다. 이벤트가 없으면 None."""
    with SessionLocal() as db:
        events = db.scalars(
            select(WebTrainingEvent.event_type)
            .where(WebTrainingEvent.session_id == session_id)
            .order_by(WebTrainingEvent.created_at)
        ).all()
    if not events:
        return None
    return score_web_events(list(events))
