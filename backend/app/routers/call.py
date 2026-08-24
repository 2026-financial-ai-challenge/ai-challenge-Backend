from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas.call import StartCallResponse
from app.services.call_service import (
    CallConfigurationError,
    PhoneNotRegisteredError,
    SessionNotFoundError,
    start_outbound_call,
)


router = APIRouter(prefix="/v1/sessions", tags=["Calls"])


@router.post("/{session_id}/calls", response_model=StartCallResponse)
async def start_call(session_id: str):
    try:
        call_id = await start_outbound_call(session_id)
    except SessionNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"message": "세션을 찾을 수 없습니다.", "code": "SESSION_NOT_FOUND"},
        )
    except PhoneNotRegisteredError:
        return JSONResponse(
            status_code=409,
            content={"message": "전화번호가 등록되지 않았습니다.", "code": "PHONE_REQUIRED"},
        )
    except CallConfigurationError as exc:
        return JSONResponse(
            status_code=503,
            content={"message": str(exc), "code": "CALL_SERVICE_NOT_CONFIGURED"},
        )
    except Exception:
        return JSONResponse(
            status_code=502,
            content={"message": "전화 발신에 실패했습니다.", "code": "CALL_START_FAILED"},
        )

    return StartCallResponse(callId=call_id, status="calling")
