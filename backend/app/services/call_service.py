import logging

from app.services.session_service import mask_phone_number


logger = logging.getLogger(__name__)


def start_training_calls(session_id: str, phone_number: str) -> None:
    """발신은 SMS 점유 인증이 끝난 뒤에만 호출한다."""
    logger.info(
        "Starting training calls session=%s phone=%s",
        session_id,
        mask_phone_number(phone_number),
    )
