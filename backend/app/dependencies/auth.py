from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.database import get_db
from app.errors import ApiError
from app.models.participant import Participant
from app.services.auth_service import decode_access_token


def get_current_participant(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> Participant:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(401, "AUTH_REQUIRED", "로그인이 필요합니다.")
    participant_id = decode_access_token(authorization.split(" ", 1)[1].strip())
    participant = db.get(Participant, participant_id)
    if participant is None or participant.password_hash is None:
        raise ApiError(401, "INVALID_ACCESS_TOKEN", "로그인이 필요합니다.")
    return participant
