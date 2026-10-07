from fastapi import APIRouter, Depends, Response

from app.dependencies.auth import get_owned_training_session
from app.models.training_session import TrainingSession
from app.schemas.web_training import (
    IssueLinkResponse,
    RecordWebEventRequest,
    WebTrainingPageResponse,
)
from app.services.web_training_service import (
    create_link,
    record_event,
    verify_link,
)


# 훈련자는 로그인 없이 문자 링크로 들어오므로, 공개 엔드포인트는 링크 토큰으로 접근을 통제한다.
router = APIRouter(prefix="/v1/web-training", tags=["WebTraining"])


@router.post("/sessions/{session_id}/link", response_model=IssueLinkResponse)
def issue_link(
    session_id: str,
    _owned_session: TrainingSession = Depends(get_owned_training_session),
):
    return IssueLinkResponse(token=create_link(session_id))


@router.get("/{token}", response_model=WebTrainingPageResponse)
def open_page(token: str):
    verify_link(token)
    return WebTrainingPageResponse()


@router.post("/{token}/events", status_code=204)
def post_event(token: str, body: RecordWebEventRequest):
    record_event(token, body.eventType)
    return Response(status_code=204)
