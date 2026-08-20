from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.participant import Participant
from app.schemas.participant import ParticipantCreate, ParticipantResponse
from app.schemas.consent import RegisterPhoneRequest, RegisterPhoneResponse
from app.services.participant_service import register_phone

router = APIRouter(
    prefix='/participants',
    tags=['Participants']
)

@router.post("", response_model=ParticipantResponse)
def create_participant(
    participant: ParticipantCreate,
    db: Session = Depends(get_db)
):
    new_participant = Participant(
        phone_number=participant.phone_number
    )

    db.add(new_participant)
    db.commit()
    db.refresh(new_participant)

    return new_participant


session_router = APIRouter(prefix="/v1/sessions", tags=["Sessions"])


@session_router.post("/{session_id}/phone", response_model=RegisterPhoneResponse)
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
