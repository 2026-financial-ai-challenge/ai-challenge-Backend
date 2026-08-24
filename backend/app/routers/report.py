from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas.report import GetReportResponse
from app.services.report_service import get_report
from app.services.session_service import get_session


router = APIRouter(prefix="/v1/sessions", tags=["Reports"])


@router.get("/{session_id}/report", response_model=GetReportResponse)
def get_session_report(session_id: str):
    if get_session(session_id) is None:
        return JSONResponse(
            status_code=404,
            content={
                "message": "세션을 찾을 수 없습니다.",
                "code": "SESSION_NOT_FOUND",
            },
        )
    return get_report(session_id)
