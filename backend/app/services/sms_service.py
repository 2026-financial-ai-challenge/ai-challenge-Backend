import logging
import os
import re

from clawops import ClawOps

from app.errors import ApiError
from app.services.session_service import mask_phone_number


logger = logging.getLogger(__name__)


def send_sms(phone_number: str, body: str) -> str:
    """ClawOps로 문자를 보내고 message_id를 반환한다. ClawOps는 국내 IP에서만 발송을 허용한다."""
    api_key = os.getenv("CLAWOPS_API_KEY", "").strip()
    account_id = os.getenv("CLAWOPS_ACCOUNT_ID", "").strip()
    from_number = os.getenv("CLAWOPS_SMS_FROM", "").strip()

    if not api_key or not account_id or not from_number:
        raise ApiError(
            500,
            "SMS_NOT_CONFIGURED",
            "ClawOps 문자 발송 설정이 완료되지 않았습니다.",
        )

    if not re.fullmatch(r"070\d{8}", re.sub(r"\D", "", from_number)):
        raise ApiError(
            500,
            "SMS_NOT_CONFIGURED",
            "CLAWOPS_SMS_FROM에 등록된 070 발신번호를 설정해 주세요.",
        )

    client = ClawOps(
        api_key=api_key,
        account_id=account_id,
    )

    try:
        message = client.messages.create(
            to=phone_number,
            from_=from_number,
            body=body,
        )
    except Exception:
        logger.exception("ClawOps SMS delivery request failed")
        raise ApiError(
            502,
            "SMS_SEND_FAILED",
            "문자 발송에 실패했습니다. 잠시 후 다시 시도해 주세요.",
        ) from None

    logger.info(
        "ClawOps SMS queued: message_id=%s phone=%s",
        message.message_id,
        mask_phone_number(phone_number),
    )
    return message.message_id


def send_verification_code(phone_number: str, code: str) -> None:
    send_sms(
        phone_number,
        f"[안심피싱] 회원가입 인증번호는 {code}입니다. 5분 안에 입력해 주세요.",
    )


def expose_dev_code() -> bool:
    return os.getenv("SMS_EXPOSE_DEV_CODE", "false").lower() == "true"

