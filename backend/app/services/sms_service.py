import logging
import os
import re

from clawops import ClawOps

from app.errors import ApiError


logger = logging.getLogger(__name__)


def send_verification_code(phone_number: str, code: str) -> None:
    api_key = os.getenv("CLAWOPS_API_KEY", "").strip()
    account_id = os.getenv("CLAWOPS_ACCOUNT_ID", "").strip()
    from_number = os.getenv("CLAWOPS_SMS_FROM", "").strip()
    if not api_key or not account_id or not from_number:
        raise ApiError(500, "SMS_NOT_CONFIGURED", "ClawOps 문자 발송 설정이 완료되지 않았습니다.")
    if not re.fullmatch(r"070\d{8}", re.sub(r"\D", "", from_number)):
        raise ApiError(500, "SMS_NOT_CONFIGURED", "CLAWOPS_SMS_FROM에 등록된 070 발신번호를 설정해 주세요.")

    try:
        message = ClawOps(api_key=api_key, account_id=account_id).messages.create(
            to=phone_number,
            from_=from_number,
            body=f"[안심피싱] 회원가입 인증번호는 {code}입니다. 5분 안에 입력해 주세요.",
        )
    except Exception:
        logger.exception("ClawOps SMS delivery request failed")
        raise ApiError(502, "SMS_SEND_FAILED", "인증번호 발송에 실패했습니다.") from None
    logger.info("ClawOps verification SMS queued: message_id=%s phone=%s", message.message_id, _mask_phone(phone_number))


def expose_dev_code() -> bool:
    return os.getenv("SMS_EXPOSE_DEV_CODE", "false").lower() == "true"


def _mask_phone(phone_number: str) -> str:
    return f"{phone_number[:3]}-****-{phone_number[-4:]}"
