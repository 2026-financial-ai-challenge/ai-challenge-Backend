import hmac
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.errors import ApiError


logger = logging.getLogger(__name__)

SOLAPI_SEND_URL = "https://api.solapi.com/messages/v4/send"


def send_otp_sms(phone_number: str, code: str) -> None:
    text = f"[보이스피싱 훈련] 인증번호는 [{code}]입니다. 3분 이내에 입력해 주세요."
    provider = _sms_provider()

    if provider == "solapi":
        _send_solapi(phone_number, text)
        return

    masked = _mask_for_log(phone_number)
    line = f"[OTP] {masked} 인증번호: {code}"
    logger.info(line)
    print(line, flush=True)


def _sms_provider() -> str:
    explicit = os.getenv("SMS_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    if os.getenv("SOLAPI_API_KEY") and os.getenv("SOLAPI_API_SECRET") and os.getenv("SOLAPI_SENDER"):
        return "solapi"
    return "log"


def _send_solapi(phone_number: str, text: str) -> None:
    api_key = os.getenv("SOLAPI_API_KEY", "")
    api_secret = os.getenv("SOLAPI_API_SECRET", "")
    sender = os.getenv("SOLAPI_SENDER", "")
    if not api_key or not api_secret or not sender:
        raise ApiError(
            500,
            "OTP_SEND_FAILED",
            "인증번호를 보내지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )

    date = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    salt = uuid.uuid4().hex
    signature = hmac.new(
        api_secret.encode(),
        f"{date}{salt}".encode(),
        hashlib.sha256,
    ).hexdigest()

    payload = json.dumps(
        {
            "message": {
                "to": phone_number,
                "from": sender,
                "text": text,
            }
        }
    ).encode()

    request = Request(
        SOLAPI_SEND_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": (
                f"HMAC-SHA256 apiKey={api_key}, date={date}, salt={salt}, signature={signature}"
            ),
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise ApiError(
                    500,
                    "OTP_SEND_FAILED",
                    "인증번호를 보내지 못했습니다. 잠시 후 다시 시도해 주세요.",
                )
    except ApiError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError):
        logger.exception("Failed to send OTP SMS via Solapi")
        raise ApiError(
            500,
            "OTP_SEND_FAILED",
            "인증번호를 보내지 못했습니다. 잠시 후 다시 시도해 주세요.",
        )


def _mask_for_log(phone_number: str) -> str:
    if len(phone_number) >= 7:
        return f"{phone_number[:3]}****{phone_number[-4:]}"
    return "****"
