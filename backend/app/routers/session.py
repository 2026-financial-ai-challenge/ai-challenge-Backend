from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas.consent import RegisterPhoneRequest, RegisterPhoneResponse
from app.services.session_service import register_phone


router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


@router.post("/{session_id}/phone", response_model=RegisterPhoneResponse)
def register_session_phone(session_id: str, body: RegisterPhoneRequest):
    try:
        session = register_phone(session_id, body.phoneNumber)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={
                "message": "올바른 휴대전화번호 형식이 아닙니다.",
                "code": "INVALID_PHONE",
            },
        )

    if session is None:
        return JSONResponse(
            status_code=404,
            content={
                "message": "세션을 찾을 수 없습니다.",
                "code": "SESSION_NOT_FOUND",
            },
        )

    return RegisterPhoneResponse(session=session)
