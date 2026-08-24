from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.schemas.consent import SubmitConsentRequest, SubmitConsentResponse
from app.services.session_service import create_session


router = APIRouter(prefix="/v1/consents", tags=["Consents"])


@router.post("", response_model=SubmitConsentResponse)
def submit_consent(body: SubmitConsentRequest):
    if not body.privacy or not body.unannouncedTraining:
        return JSONResponse(
            status_code=400,
            content={
                "message": "필수 동의 항목에 모두 동의해야 합니다.",
                "code": "CONSENT_REQUIRED",
            },
        )

    session = create_session(
        privacy=body.privacy,
        unannounced_training=body.unannouncedTraining,
    )
    return SubmitConsentResponse(sessionId=session.id)
