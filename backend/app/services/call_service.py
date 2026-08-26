import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from threading import Lock, Thread

from sqlalchemy import select

from app.database import SessionLocal
from app.models.call import Call
from app.services.report_service import (
    bind_call,
    build_draft_report,
    get_report,
    register_transcript_listener,
    request_clawops_transcript,
)
from app.services.session_service import (
    attach_call,
    get_phone_number,
    get_session,
    mask_phone_number,
    update_call_status,
    update_report_status,
)
from app.training.scenarios import get_call_scenario


logger = logging.getLogger(__name__)
_outbound_lock = Lock()


class CallServiceError(Exception):
    pass


class SessionNotFoundError(CallServiceError):
    pass


class PhoneNotRegisteredError(CallServiceError):
    pass


class CallConfigurationError(CallServiceError):
    pass


class CallInProgressError(CallServiceError):
    pass


def trainee_spoke(session_id: str) -> bool:
    return any(
        turn.role == "user" and turn.text.strip()
        for turn in get_report(session_id).turns
    )


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CallConfigurationError(f"{name} is not set")
    cleaned = "".join(ch for ch in value if ch.isascii() and not ch.isspace())
    if cleaned != value:
        logger.warning("%s had non-ASCII or whitespace characters; they were stripped", name)
        os.environ[name] = cleaned
    return cleaned


def _supported_kwargs(cls, **kwargs):
    params = inspect.signature(cls).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


def _tts_voice_id(scenario) -> str:
    override = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if override:
        return "".join(ch for ch in override if ch.isascii() and not ch.isspace())
    if scenario.tts_voice_id:
        return scenario.tts_voice_id
    return _require_env("ELEVENLABS_VOICE_ID")


def _tts_stability(scenario) -> float:
    raw = os.getenv("ELEVENLABS_STABILITY", "").strip()
    if raw:
        return float(raw)
    return scenario.tts_stability


def phone_system_prompt(scenario) -> str:
    return (
        f"{scenario.system_prompt}\n\n"
        "[전화 연결]\n"
        f"- 인사말은 이미 나갔다: {scenario.opening_line}\n"
        "- 인사말을 다시 하지 않는다. 상대 말에 이어서 본론만 말한다.\n"
        "- 한 응답은 문장 둘까지. 성함은 받아 내고 생년월일은 묻지 마라.\n"
        "- 물러지거나 허락을 구하지 마라. 지금 확인한다고 단정해서 말한다.\n"
        "- 상대가 안 들린다, 안 들어간다, 안들리세요라고 하면 "
        "상대 목소리가 안 들린다고 하지 마라. 짧게 이어서 본론을 말한다.\n"
        f"- 상대 발화 기준으로 최대 {scenario.max_turns}번이다.\n"
        "- 상대가 끊겠다고 한 첫 번째는 붙잡고 본론을 이어 간다. 두 번째에 hang_up 한다.\n"
        "- 다른 번호로 전화를 돌리지 않는다."
    )


def build_pipeline_session(scenario):
    try:
        from clawops.agent.pipeline import (
            DeepgramSTT,
            ElevenLabsTTS,
            OpenAILLM,
        )
        from app.training.deepgram_stt import PhoneDeepgramSTT
        from app.training.pipeline_session import PhonePipelineSession
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps pipeline extras are missing; rebuild with "
            "clawops[agent,openai,deepgram,elevenlabs]"
        ) from exc

    voice_id = _tts_voice_id(scenario)
    session_kwargs = _supported_kwargs(
        PhonePipelineSession,
        system_prompt=phone_system_prompt(scenario),
        stt=PhoneDeepgramSTT(
            **_supported_kwargs(
                DeepgramSTT,
                language=os.getenv("DEEPGRAM_LANGUAGE", "ko").strip() or "ko",
                model=os.getenv("DEEPGRAM_MODEL", "nova-2").strip() or "nova-2",
                endpointing=int(os.getenv("STT_ENDPOINTING_MS", "400")),
                utterance_end_ms=int(os.getenv("STT_UTTERANCE_END_MS", "1200")),
            )
        ),
        llm=OpenAILLM(
            **_supported_kwargs(
                OpenAILLM,
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
                temperature=0.55,
                max_tokens=180,
            )
        ),
        tts=ElevenLabsTTS(
            **_supported_kwargs(
                ElevenLabsTTS,
                voice_id=voice_id,
                model=os.getenv("ELEVENLABS_MODEL_ID", "eleven_turbo_v2_5").strip()
                or "eleven_turbo_v2_5",
                stability=_tts_stability(scenario),
                similarity_boost=scenario.tts_similarity_boost,
                language_code="ko",
            )
        ),
        greeting=True,
        opening_line=scenario.opening_line,
        max_turns=scenario.max_turns,
    )
    return PhonePipelineSession(**session_kwargs)


def _make_clawops_agent(from_number: str, scenario):
    try:
        from clawops.agent import BuiltinTool, ClawOpsAgent
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps Agent SDK is not installed; rebuild the backend image"
        ) from exc

    kwargs = {
        "from_": from_number,
        "builtin_tools": [BuiltinTool.HANG_UP],
    }
    if "session_factory" in inspect.signature(ClawOpsAgent).parameters:
        kwargs["session_factory"] = lambda: build_pipeline_session(scenario)
    else:
        kwargs["session"] = build_pipeline_session(scenario)
    return ClawOpsAgent(**_supported_kwargs(ClawOpsAgent, **kwargs))


def _pipeline_configured() -> bool:
    return all(
        os.getenv(name, "").strip()
        for name in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY")
    )


def _make_realtime_agent(from_number: str, scenario):
    try:
        from clawops.agent import ClawOpsAgent, OpenAIRealtime
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps OpenAI Realtime dependencies are missing; rebuild the backend image"
        ) from exc

    logger.warning(
        "DEEPGRAM_API_KEY or ELEVENLABS_API_KEY is missing; "
        "falling back to OpenAI Realtime"
    )
    return ClawOpsAgent(
        from_=from_number,
        session=OpenAIRealtime(
            system_prompt=scenario.system_prompt,
            voice=os.getenv("CLAWOPS_VOICE", "marin"),
            language="ko",
        ),
    )


async def start_outbound_call(session_id: str) -> str:
    agent, call_session = await _create_outbound_call(session_id)
    asyncio.create_task(_monitor_call(session_id, agent, call_session))
    return call_session.call_id


async def _create_outbound_call(session_id: str):
    session = get_session(session_id)
    if session is None:
        raise SessionNotFoundError
    if session.callStatus == "calling":
        raise CallInProgressError

    phone_number = get_phone_number(session_id)
    if phone_number is None:
        raise PhoneNotRegisteredError

    _require_env("CLAWOPS_API_KEY")
    _require_env("CLAWOPS_ACCOUNT_ID")
    from_number = _require_env("CLAWOPS_PHONE_NUMBER")
    _require_env("OPENAI_API_KEY")

    try:
        scenario = get_call_scenario()
    except Exception as exc:
        raise CallConfigurationError(str(exc)) from exc

    logger.info(
        "Starting pipeline call session=%s scenario=%s",
        session_id,
        scenario.id,
    )

    agent = (
        _make_clawops_agent(from_number, scenario)
        if _pipeline_configured()
        else _make_realtime_agent(from_number, scenario)
    )
    register_transcript_listener(agent, session_id)

    try:
        call_session = await agent.call(phone_number, timeout=30)
    except TimeoutError:
        await agent.disconnect()
        raise CallConfigurationError(
            "지금은 전화를 걸 수 없습니다. 잠시 후 다시 시도해 주세요."
        ) from None
    except Exception as exc:
        await agent.disconnect()
        message = str(exc)
        if "Concurrent call limit" in message or "429" in message:
            logger.error("ClawOps outbound rejected: %s", message)
            raise CallConfigurationError(
                "지금은 통화 연결이 혼잡합니다. 잠시 후 다시 시도해 주세요."
            ) from None
        raise

    bind_call(session_id, call_session.call_id)
    attach_call(session_id, call_session.call_id)
    return agent, call_session


async def _monitor_call(session_id: str, agent, call_session) -> None:
    try:
        await call_session.wait()
        if trainee_spoke(session_id):
            update_call_status(session_id, "completed")
            _complete_call(call_session.call_id)
            try:
                await asyncio.wait_for(build_draft_report(session_id), timeout=20)
            except Exception:
                logger.exception("Draft report failed: session_id=%s", session_id)
            try:
                await request_clawops_transcript(call_session.call_id)
            except Exception:
                logger.exception(
                    "ClawOps transcript request failed: session_id=%s",
                    session_id,
                )
        else:
            update_call_status(session_id, "missed")
            _fail_call(call_session.call_id, "no_answer")
            update_report_status(session_id, "none")
            logger.info("Training call missed session=%s", session_id)
    except Exception:
        update_call_status(session_id, "failed")
        _fail_call(call_session.call_id, "ClawOps call failed")
        logger.exception("ClawOps call failed: session_id=%s", session_id)
    finally:
        await agent.disconnect()


def start_training_calls(session_id: str, phone_number: str) -> None:
    """발신은 백그라운드에서 진행한다. HTTP 요청을 ClawOps 연결에 묶지 않는다."""
    logger.info(
        "Starting training calls session=%s phone=%s",
        session_id,
        mask_phone_number(phone_number),
    )
    update_call_status(session_id, "waiting")
    Thread(
        target=_run_training_call,
        args=(session_id,),
        daemon=True,
    ).start()


def _run_training_call(session_id: str) -> None:
    if not _outbound_lock.acquire(timeout=20):
        update_call_status(session_id, "failed")
        logger.warning(
            "Previous ClawOps agent still running; skip session=%s", session_id
        )
        return
    try:
        asyncio.run(_start_and_monitor_call(session_id))
    except Exception:
        update_call_status(session_id, "failed")
        logger.exception("Failed to start ClawOps call: session_id=%s", session_id)
    finally:
        _outbound_lock.release()


async def _start_and_monitor_call(session_id: str) -> None:
    agent, call_session = await _create_outbound_call(session_id)
    await _monitor_call(session_id, agent, call_session)


def _complete_call(clawops_call_id: str) -> None:
    with SessionLocal.begin() as db:
        call = db.scalar(
            select(Call).where(Call.clawops_call_id == clawops_call_id)
        )
        if call is not None:
            call.status = "completed"
            call.completed_at = datetime.now(timezone.utc)


def _fail_call(clawops_call_id: str, reason: str) -> None:
    with SessionLocal.begin() as db:
        call = db.scalar(
            select(Call).where(Call.clawops_call_id == clawops_call_id)
        )
        if call is not None:
            call.status = "failed"
            call.failure_reason = reason
            call.completed_at = datetime.now(timezone.utc)
