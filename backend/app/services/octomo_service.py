import logging
import os

import httpx

from app.errors import ApiError

logger = logging.getLogger(__name__)

OCTOMO_URL = "https://api.octoverse.kr/octomo/v1/public/message/exists"
DEFAULT_SEND_TO = "16663538"


def send_to_number() -> str:
    digits = os.getenv("OCTOMO_SEND_TO", DEFAULT_SEND_TO).replace("-", "").strip()
    return digits or DEFAULT_SEND_TO


def message_exists(mobile_num: str, text: str, within_minutes: int = 5) -> bool:
    api_key = os.getenv("OCTOMO_API_KEY", "").strip()
    if not api_key:
        raise ApiError(
            500,
            "OCTOMO_NOT_CONFIGURED",
            "옥토모 API 키가 설정되지 않았습니다.",
        )

    try:
        response = httpx.post(
            OCTOMO_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Octomo {api_key}",
            },
            json={
                "mobileNum": mobile_num,
                "text": text,
                "withinMinutes": within_minutes,
            },
            timeout=10.0,
            trust_env=False,
        )
    except httpx.HTTPError:
        logger.exception("Octomo request failed")
        raise ApiError(
            502,
            "OTP_SEND_FAILED",
            "번호 확인 서비스에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.",
        ) from None

    logger.info("Octomo exists status=%s body=%s", response.status_code, response.text[:300])

    if response.status_code in {401, 403}:
        raise ApiError(
            502,
            "OTP_SEND_FAILED",
            "번호 확인 서비스 인증에 실패했습니다. OCTOMO_API_KEY를 확인해 주세요.",
        )
    if response.status_code == 429:
        raise ApiError(
            429,
            "OTP_RATE_LIMITED",
            "인증 확인 횟수를 초과했습니다. 잠시 후 다시 시도해 주세요.",
        )
    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        raise ApiError(
            502,
            "OTP_SEND_FAILED",
            "번호 확인에 실패했습니다. 잠시 후 다시 시도해 주세요.",
        )

    try:
        data = response.json()
    except ValueError:
        logger.warning("Octomo returned non-JSON body")
        return False

    return bool(data.get("exists") or data.get("verified"))
