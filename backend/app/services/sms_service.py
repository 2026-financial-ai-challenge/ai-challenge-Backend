import logging
import os


logger = logging.getLogger(__name__)


def send_verification_code(phone_number: str, code: str) -> None:
    """Development transport. Replace this function with the production SMS provider."""
    provider = os.getenv("SMS_PROVIDER", "console")
    if provider != "console":
        raise RuntimeError(f"Unsupported SMS_PROVIDER: {provider}")
    logger.info("[SMS console] verification code for %s: %s", phone_number, code)


def expose_dev_code() -> bool:
    return os.getenv("SMS_EXPOSE_DEV_CODE", "true").lower() == "true"
