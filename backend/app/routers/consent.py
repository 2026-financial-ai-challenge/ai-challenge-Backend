from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.schemas.consent import SubmitConsentRequest, SubmitConsentResponse
from app.services.session_service import create_session
from app.dependencies.auth import get_current_participant
from app.models.participant import Participant
from app.services.call_service import start_training_calls


router = APIRouter(prefix="/v1/consents", tags=["Consents"])


@router.post("", response_model=SubmitConsentResponse)
def submit_consent(
    body: SubmitConsentRequest,
    participant: Participant = Depends(get_current_participant),
):
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
        participant_id=participant.id,
    )
    start_training_calls(session.id, participant.phone_number)
    return SubmitConsentResponse(sessionId=session.id)
