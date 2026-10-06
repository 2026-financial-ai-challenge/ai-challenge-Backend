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
    """ClawOps가 서명한 공개 URL. 프록시 뒤에서는 request.url과 다르다."""
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if public_base_url:
        return f"{public_base_url}{request.url.path}"
    return str(request.url)


@router.post("/status", status_code=status.HTTP_204_NO_CONTENT)
async def receive_call_status_webhook(request: Request) -> Response:
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

    callback_status = params.get("CallStatus", "").strip()
    logger.info(
        "Received ClawOps status webhook: call_id=%s CallStatus=%s fields=%s",
        call_id,
        callback_status or "-",
        ",".join(sorted(params)),
    )
    try:
        from app.services.call_service import handle_call_status_event

        await handle_call_status_event(call_id, callback_status=callback_status)
    except Exception:
        logger.exception("Failed to apply call status: call_id=%s", call_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/transcript", status_code=status.HTTP_204_NO_CONTENT)
async def receive_transcript_webhook(request: Request) -> Response:
    body = await request.body()
    params = dict(parse_qsl(body.decode(), keep_blank_values=True))
    event = params.get("Event", "")

    if event not in _EVENT_REQUIRED_FIELDS:
        # 처리하지 않는 이벤트(콘솔 테스트 등)도 204로 받는다. 실패 응답이 쌓이면 ClawOps가 웹훅 전체를 끈다.
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
