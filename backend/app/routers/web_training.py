from fastapi import APIRouter, Depends, Response

from app.dependencies.auth import get_owned_training_session
from app.models.training_session import TrainingSession
from app.schemas.web_training import (
    RecordWebEventRequest,
    WebTrainingPageResponse,
)
from app.services.web_training_service import (
    create_link,
    record_event,
    verify_link,
)


# 훈련자는 로그인 상태가 아니라 문자 링크로 진입한다. 그래서 이 라우터의 공개
# 엔드포인트는 Bearer 토큰이 아니라 추측 불가능한 링크 토큰으로 접근을 통제한다.
router = APIRouter(prefix="/v1/web-training", tags=["WebTraining"])


class IssueLinkResponse(WebTrainingPageResponse):
    token: str


@router.post("/sessions/{session_id}/link", response_model=IssueLinkResponse)
def issue_link(
    session_id: str,
    _owned_session: TrainingSession = Depends(get_owned_training_session),
):
    """세션 소유자가 웹 훈련 링크를 발급한다 (Bearer 필수)."""
    token = create_link(session_id)
    return IssueLinkResponse(token=token)


@router.get("/{token}", response_model=WebTrainingPageResponse)
def open_page(token: str):
    """``/t/{token}`` 페이지 진입 — 링크 유효성만 확인한다."""
    verify_link(token)
    return WebTrainingPageResponse()


@router.post("/{token}/events", status_code=204)
def post_event(token: str, body: RecordWebEventRequest):
    """훈련자의 웹 행동 1건을 기록한다."""
    record_event(token, body.eventType)
    return Response(status_code=204)
