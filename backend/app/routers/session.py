from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.schemas.consent import (
    GetSessionResponse,
    RequestOtpRequest,
    RequestOtpResponse,
    VerifyOtpRequest,
)
from app.services.otp_service import request_otp, verify_otp
from app.services.session_service import get_session


router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is None:
        return "unknown"
    return request.client.host


@router.get("/{session_id}", response_model=GetSessionResponse)
def get_session_by_id(session_id: str):
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


@router.post("/{session_id}/phone/otp", response_model=RequestOtpResponse)
def request_session_phone_otp(
    session_id: str, body: RequestOtpRequest, request: Request
):
    return request_otp(session_id, body.phoneNumber, client_ip(request))


@router.post("/{session_id}/phone/verify", response_model=GetSessionResponse)
def verify_session_phone_otp(session_id: str, body: VerifyOtpRequest, request: Request):
    session = verify_otp(session_id, body.phoneNumber, body.code, client_ip(request))
    return GetSessionResponse(session=session)
