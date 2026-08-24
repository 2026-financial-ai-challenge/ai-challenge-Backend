from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.participant import Participant
from app.schemas.participant import ParticipantCreate, ParticipantResponse


router = APIRouter(prefix="/participants", tags=["Participants"])


@router.post("", response_model=ParticipantResponse)
def create_participant(
    participant: ParticipantCreate,
    db: Session = Depends(get_db),
):
    new_participant = Participant(phone_number=participant.phone_number)
    db.add(new_participant)
    db.commit()
    db.refresh(new_participant)
    return new_participant
