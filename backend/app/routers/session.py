from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.schemas.consent import GetSessionResponse, ListSessionsResponse
from app.services.session_service import get_session, list_sessions_for_participant
from app.dependencies.auth import get_current_participant, get_owned_training_session
from app.models.participant import Participant
from app.models.training_session import TrainingSession


router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


@router.get("", response_model=ListSessionsResponse)
def list_my_sessions(participant: Participant = Depends(get_current_participant)):
    return ListSessionsResponse(sessions=list_sessions_for_participant(participant.id))


@router.get("/{session_id}", response_model=GetSessionResponse)
def get_session_by_id(
    session_id: str,
    _owned_session: TrainingSession = Depends(get_owned_training_session),
):
    session = get_session(session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={
                "message": "세션을 찾을 수 없습니다.",
                "code": "SESSION_NOT_FOUND",
            },
        )

    return GetSessionResponse(session=session)
