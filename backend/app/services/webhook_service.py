import base64
import hashlib
import hmac
import os

from sqlalchemy import select

from app.database import SessionLocal
from app.models.transcript_event import TranscriptEvent


def verify_clawops_signature(
    *,
    url: str,
    params: dict[str, str],
    signature: str | None,
) -> bool:
    signing_secret = os.getenv("CLAWOPS_WEBHOOK_SIGNING_SECRET", "").strip()
    if not signing_secret:
        return True
    if not signature:
        return False

    data = url + "".join(
        f"{key}{value}" for key, value in sorted(params.items())
    )
    expected = base64.b64encode(
        hmac.new(
            signing_secret.encode(),
            data.encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    return hmac.compare_digest(signature, expected)


def save_transcript_event(params: dict[str, str]) -> None:
    with SessionLocal.begin() as db:
        event = db.scalar(
            select(TranscriptEvent).where(
                TranscriptEvent.clawops_call_id == params["CallId"],
                TranscriptEvent.event_type == params["Event"],
            )
        )
        if event is None:
            db.add(
                TranscriptEvent(
                    clawops_call_id=params["CallId"],
                    event_type=params["Event"],
                    payload=params.copy(),
                )
            )
        else:
            event.payload = params.copy()
