import asyncio
import logging
import os

from app.services.session_service import (
    get_phone_number,
    get_session,
    update_call_status,
)


logger = logging.getLogger(__name__)


class CallServiceError(Exception):
    pass


class SessionNotFoundError(CallServiceError):
    pass


class PhoneNotRegisteredError(CallServiceError):
    pass


class CallConfigurationError(CallServiceError):
    pass


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CallConfigurationError(f"{name} is not set")
    return value


async def start_outbound_call(session_id: str) -> str:
    if get_session(session_id) is None:
        raise SessionNotFoundError

    phone_number = get_phone_number(session_id)
    if phone_number is None:
        raise PhoneNotRegisteredError

    _require_env("CLAWOPS_API_KEY")
    _require_env("CLAWOPS_ACCOUNT_ID")
    from_number = _require_env("CLAWOPS_PHONE_NUMBER")
    _require_env("OPENAI_API_KEY")

    try:
        from clawops.agent import ClawOpsAgent, OpenAIRealtime
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps Agent SDK is not installed; rebuild the backend image"
        ) from exc

    agent = ClawOpsAgent(
        from_=from_number,
        session=OpenAIRealtime(
            system_prompt=os.getenv(
                "CLAWOPS_SYSTEM_PROMPT",
                "보이스피싱 대응 훈련을 진행합니다. 한국어로 응대합니다.",
            ),
            voice=os.getenv("CLAWOPS_VOICE", "marin"),
            language="ko",
        ),
    )

    try:
        call_session = await agent.call(phone_number, timeout=30)
    except Exception:
        await agent.disconnect()
        raise

    update_call_status(session_id, "calling")
    asyncio.create_task(_monitor_call(session_id, agent, call_session))
    return call_session.call_id


async def _monitor_call(session_id: str, agent, call_session) -> None:
    try:
        await call_session.wait()
        update_call_status(session_id, "completed")
    except Exception:
        update_call_status(session_id, "waiting")
        logger.exception("ClawOps call failed: session_id=%s", session_id)
    finally:
        await agent.disconnect()
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
