from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.schemas.consent import GetSessionResponse
from app.services.session_service import get_session
from app.dependencies.auth import get_owned_training_session
from app.models.training_session import TrainingSession


router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


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
