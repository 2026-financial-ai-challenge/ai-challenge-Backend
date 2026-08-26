from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas.call import StartCallResponse
from app.services.call_service import start_training_calls
from app.services.session_service import get_phone_number, get_session


router = APIRouter(prefix="/v1/sessions", tags=["Calls"])


@router.post("/{session_id}/calls", response_model=StartCallResponse)
def start_call(session_id: str):
    session = get_session(session_id)
    if session is None:
        return JSONResponse(
            status_code=404,
            content={"message": "세션을 찾을 수 없습니다.", "code": "SESSION_NOT_FOUND"},
        )
    if session.callStatus == "calling":
        return JSONResponse(
            status_code=409,
            content={
                "message": "이미 훈련 전화를 걸고 있습니다. 잠시만 기다려 주세요.",
                "code": "CALL_IN_PROGRESS",
            },
        )

    phone_number = get_phone_number(session_id)
    if phone_number is None:
        return JSONResponse(
            status_code=409,
            content={"message": "전화번호가 등록되지 않았습니다.", "code": "PHONE_REQUIRED"},
        )

    start_training_calls(session_id, phone_number)
    return StartCallResponse(status="waiting")
