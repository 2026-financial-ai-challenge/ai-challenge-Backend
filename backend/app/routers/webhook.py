import logging
import os
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.services.report_service import handle_transcript_event
from app.services.webhook_service import (
    save_transcript_event,
    verify_clawops_signature,
)


router = APIRouter(prefix="/v1/webhooks/clawops", tags=["ClawOps Webhooks"])
logger = logging.getLogger(__name__)

_COMMON_REQUIRED_FIELDS = {
    "Event",
    "CallId",
    "AccountId",
    "From",
    "To",
    "Direction",
    "Timestamp",
}
_EVENT_REQUIRED_FIELDS = {
    "transcript.completed": {"TranscriptUrl", "DurationSec", "SegmentCount"},
    "transcript.failed": {"Stage", "ErrorMessage"},
}


@router.post("/transcript", status_code=status.HTTP_204_NO_CONTENT)
async def receive_transcript_webhook(request: Request) -> Response:
    body = await request.body()
    params = dict(parse_qsl(body.decode(), keep_blank_values=True))
    event = params.get("Event", "")

    if event not in _EVENT_REQUIRED_FIELDS:
        raise HTTPException(status_code=400, detail="Unsupported transcript event")

    missing = (_COMMON_REQUIRED_FIELDS | _EVENT_REQUIRED_FIELDS[event]) - params.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(sorted(missing))}",
        )

    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    webhook_url = (
        f"{public_base_url}{request.url.path}"
        if public_base_url
        else str(request.url)
    )
    if not verify_clawops_signature(
        url=webhook_url,
        params=params,
        signature=request.headers.get("X-Signature"),
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    save_transcript_event(params)
    logger.info(
        "Received ClawOps transcript webhook: event=%s call_id=%s",
        event,
        params["CallId"],
    )
    try:
        await handle_transcript_event(params)
    except Exception:
        logger.exception(
            "Failed to build final report from webhook: call_id=%s",
            params["CallId"],
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
