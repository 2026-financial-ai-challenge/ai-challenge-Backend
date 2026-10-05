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


def _webhook_url(request: Request) -> str:
    """The URL ClawOps signed. Behind a proxy request.url is not it."""
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base_url:
        return f"{public_base_url}{request.url.path}"
    return str(request.url)


@router.post("/status", status_code=status.HTTP_204_NO_CONTENT)
async def receive_call_status_webhook(request: Request) -> Response:
    """Call status for managed-agent calls (CALL_AGENT_MODE=managed).

    Only CallId is read from the body. ClawOps does not publish a schema for
    this payload, and the call's own API record is typed, so the callback is
    treated as a trigger and the status is fetched rather than parsed.
    """
    body = await request.body()
    params = dict(parse_qsl(body.decode(), keep_blank_values=True))
    call_id = params.get("CallId", "").strip()
    if not call_id:
        raise HTTPException(status_code=400, detail="Missing required field: CallId")

    if not verify_clawops_signature(
        url=_webhook_url(request),
        params=params,
        signature=request.headers.get("X-Signature"),
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    logger.info("Received ClawOps status webhook: call_id=%s", call_id)
    try:
        from app.services.call_service import handle_call_status_event

        await handle_call_status_event(call_id)
    except Exception:
        logger.exception("Failed to apply call status: call_id=%s", call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/transcript", status_code=status.HTTP_204_NO_CONTENT)
async def receive_transcript_webhook(request: Request) -> Response:
    body = await request.body()
    params = dict(parse_qsl(body.decode(), keep_blank_values=True))
    event = params.get("Event", "")

    if event not in _EVENT_REQUIRED_FIELDS:
        # Acknowledge anything we do not act on -- the console's test ping
        # ("test"), an event subscribed by mistake, one ClawOps adds later.
        # Answering 400 marks the delivery failed, and enough failures get the
        # whole webhook disabled, taking transcript.completed down with it.
        logger.info("Ignoring ClawOps event this endpoint does not handle: %s", event)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    missing = (_COMMON_REQUIRED_FIELDS | _EVENT_REQUIRED_FIELDS[event]) - params.keys()
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required fields: {', '.join(sorted(missing))}",
        )

    if not verify_clawops_signature(
        url=_webhook_url(request),
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
