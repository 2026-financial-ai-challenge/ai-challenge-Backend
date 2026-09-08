from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies.auth import get_current_participant
from app.errors import ApiError
from app.models.participant import Participant
from app.schemas.consent import SubmitConsentRequest, SubmitConsentResponse
from app.services.auth_service import record_training_consent
from app.services.call_service import start_training_calls
from app.services.session_service import create_session_for_participant


router = APIRouter(prefix="/v1/consents", tags=["Consents"])


@router.post("", response_model=SubmitConsentResponse)
def submit_consent(
    body: SubmitConsentRequest,
    participant: Participant = Depends(get_current_participant),
    db: Session = Depends(get_db),
):
    if not participant.has_training_consent():
        if not body.privacy or not body.unannouncedTraining:
            raise ApiError(400, "CONSENT_REQUIRED", "필수 동의 항목에 모두 동의해야 합니다.")
        record_training_consent(participant)
        db.commit()
        db.refresh(participant)

    session = create_session_for_participant(participant)
    start_training_calls(session.id, participant.phone_number)
    return SubmitConsentResponse(sessionId=session.id)
