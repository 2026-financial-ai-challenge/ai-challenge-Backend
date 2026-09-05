import asyncio
import inspect
import logging
import os
from datetime import datetime, timezone
from threading import Lock, Thread

from sqlalchemy import select

from app.database import SessionLocal
from app.models.call import Call
from app.models.transcript_turn import TranscriptTurnRecord
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
from app.training.scenarios import get_runtime_scenario


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


def trainee_spoke(session_id: str, call_id: str | None = None) -> bool:
    if call_id is not None:
        return _call_has_turn(session_id, call_id, role="user")
    return any(
        turn.role == "user" and turn.text.strip()
        for turn in get_report(session_id).turns
    )


def call_had_transcript(session_id: str, call_id: str | None = None) -> bool:
    if call_id is not None:
        return _call_has_turn(session_id, call_id)
    return any(turn.text.strip() for turn in get_report(session_id).turns)


def _call_has_turn(
    session_id: str,
    call_id: str,
    *,
    role: str | None = None,
) -> bool:
    with SessionLocal() as db:
        statement = (
            select(TranscriptTurnRecord.id)
            .join(Call, TranscriptTurnRecord.call_id == Call.id)
            .where(
                Call.session_id == session_id,
                Call.clawops_call_id == call_id,
                TranscriptTurnRecord.text != "",
            )
            .limit(1)
        )
        if role is not None:
            statement = statement.where(TranscriptTurnRecord.role == role)
        return db.scalar(statement) is not None


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CallConfigurationError(f"{name} is not set")
    cleaned = "".join(ch for ch in value if ch.isascii() and not ch.isspace())
    if cleaned != value:
        logger.warning("%s had non-ASCII or whitespace characters; they were stripped", name)
        os.environ[name] = cleaned
    return cleaned


def _call_llm_provider() -> str:
    """Which LLM answers the trainee during the live call.

    'openai' (default) or 'gemini'. Independent from SCENARIO_LLM_PROVIDER
    (ai/scenarios/generator.py) — that one only writes the scenario before
    the call starts; this one drives in-call responses, so a quota-exhausted
    OpenAI key can be swapped out for both without touching the other.
    """
    return os.getenv("CALL_LLM_PROVIDER", "openai").strip().lower() or "openai"


def _require_call_llm_key() -> None:
    if _call_llm_provider() == "gemini":
        _require_env("GEMINI_API_KEY")
    else:
        _require_env("OPENAI_API_KEY")


def _supported_kwargs(cls, **kwargs):
    params = inspect.signature(cls).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {key: value for key, value in kwargs.items() if key in params}


def _tts_voice_id(scenario) -> str:
    random_enabled = os.getenv(
        "ELEVENLABS_VOICE_RANDOM", "false"
    ).strip().lower() in {"1", "true", "yes", "on"}

    if random_enabled:
        from ai.voices import random_voice_id

        return random_voice_id()

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


def _tts_style(scenario) -> float:
    """Voice style exaggeration. 0 (ElevenLabs' default) reads as a flat script."""
    raw = os.getenv("ELEVENLABS_STYLE", "").strip()
    if raw:
        return float(raw)
    return getattr(scenario, "tts_style", 0.15)


def _tts_speed(scenario) -> float:
    """Slightly under 1.0 so the caller doesn't sound rushed."""
    raw = os.getenv("ELEVENLABS_SPEED", "").strip()
    if raw:
        return float(raw)
    return getattr(scenario, "tts_speed", 0.94)


def phone_system_prompt(scenario) -> str:
    """Append the phone-transport rules to a scenario's own prompt.

    Kept short on purpose: style and output-length rules already live in the
    scenario prompt (ai/scenarios/playbook.py), and repeating them here only
    grows the prefix every live turn has to re-read.
    """
    return (
        f"{scenario.system_prompt}\n\n"
        "[전화 연결]\n"
        f"- 인사말은 이미 나갔다: {scenario.opening_line}\n"
        "- 인사말을 다시 하지 않는다. 상대 말에 이어서 본론만 말한다.\n"
        "- 한 응답은 문장 둘까지다. 물러서거나 허락을 구하지 않는다.\n"
        "- 상대가 안 들린다고 하면 짧게 이어서 본론을 다시 말한다.\n"
        f"- 상대 발화 기준으로 최대 {scenario.max_turns}번이다.\n"
        "- 상대가 끊겠다고 한 첫 번째는 붙잡고 본론을 이어 간다. 두 번째에 hang_up 한다.\n"
        "- 다른 번호로 전화를 돌리지 않는다."
    )


def build_pipeline_session(scenario):
    try:
        from clawops.agent.pipeline import (
            DeepgramSTT,
            ElevenLabsTTS,
            GeminiLLM,
            OpenAILLM,
        )
        from app.training.deepgram_stt import PhoneDeepgramSTT
        from app.training.gemini_llm import PhoneGeminiLLM
        from app.training.pipeline_session import PhonePipelineSession
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps pipeline extras are missing; rebuild with "
            "clawops[agent,openai,gemini,deepgram,elevenlabs]"
        ) from exc

    voice_id = _tts_voice_id(scenario)

    # Built separately so we can see which naturalness settings the installed
    # ClawOps ElevenLabsTTS actually accepts. _supported_kwargs() drops unknown
    # ones silently, which would otherwise make a voice-tuning change look
    # applied when it never reached ElevenLabs.
    # eleven_flash_v2_5 is ElevenLabs' lowest-latency Korean-capable model.
    # The value itself lives in ai/.env, which app/main.py also loads, so both
    # the phone pipeline and the local harness read one copy. This default is
    # only the floor for deployments that ship no .env at all.
    tts_requested = dict(
        voice_id=voice_id,
        model=os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip()
        or "eleven_flash_v2_5",
        stability=_tts_stability(scenario),
        similarity_boost=scenario.tts_similarity_boost,
        # style / use_speaker_boost / speed are NOT in the installed
        # ElevenLabsTTS signature (verified against clawops 0.46.1), so they
        # are sent only so the warning below names them if a future build
        # starts accepting them. Scenario tts_style / tts_speed currently
        # affect the local ai/ pipeline only.
        style=_tts_style(scenario),
        use_speaker_boost=True,
        speed=_tts_speed(scenario),
        output_format=os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_24000").strip()
        or "pcm_24000",
        language_code="ko",
    )
    tts_kwargs = _supported_kwargs(ElevenLabsTTS, **tts_requested)
    dropped = sorted(set(tts_requested) - set(tts_kwargs))
    if dropped:
        logger.warning(
            "ElevenLabsTTS ignored unsupported voice settings: %s "
            "(the installed clawops build does not accept them)",
            ", ".join(dropped),
        )

    session_kwargs = _supported_kwargs(
        PhonePipelineSession,
        system_prompt=phone_system_prompt(scenario),
        stt=PhoneDeepgramSTT(
            **_supported_kwargs(
                DeepgramSTT,
                language=os.getenv("DEEPGRAM_LANGUAGE", "ko").strip() or "ko",
                model=os.getenv("DEEPGRAM_MODEL", "nova-2").strip() or "nova-2",
                # This is the real handle on dead air. A normal turn ends on
                # Deepgram's speech_final, which fires after this much silence
                # (see UtteranceAssembler in app/training/deepgram_stt.py), so
                # perceived latency is floored here -- not at utterance_end_ms.
                # 250-300 is noticeably snappier; too low cuts off slow speakers.
                endpointing=int(os.getenv("STT_ENDPOINTING_MS", "400")),
                # Backstop only, for the case where Deepgram never sends
                # speech_final. It bounds the worst turn, not the typical one.
                utterance_end_ms=int(os.getenv("STT_UTTERANCE_END_MS", "1000")),
            )
        ),
        llm=_build_call_llm(PhoneGeminiLLM, OpenAILLM),
        tts=ElevenLabsTTS(**tts_kwargs),
        greeting=True,
        opening_line=scenario.opening_line,
        max_turns=scenario.max_turns,
        quick_replies=getattr(scenario, "quick_replies", ()),
        hangup_line=getattr(scenario, "hangup_line", ""),
        reflex_budget=int(os.getenv("CALL_REFLEX_BUDGET", "3")),
        stall_line=os.getenv("CALL_STALL_LINE", "").strip(),
    )
    return PhonePipelineSession(**session_kwargs)


def _call_max_tokens(provider: str) -> int:
    """Output cap for one live turn.

    A reply is two short Korean sentences, roughly 40-60 tokens, so 120 is
    ample for OpenAI and keeps a stray long answer from running for seconds
    of TTS.

    Gemini gets a much higher ceiling on purpose. Gemini counts hidden
    thinking tokens against the output budget, so a tight cap can be spent
    entirely on reasoning and return empty content -- which on a phone call
    is dead silence, not a slow answer. ai/scenarios/generator.py:31-34
    records exactly that ("sometimes empty content") on non-lite models.
    Length is already controlled by the prompt rule in
    ai/scenarios/playbook.py ("짧은 문장 두 개까지"), so the high cap costs
    nothing in practice. Drop it once you have confirmed from usage metadata
    that thinking really is off.
    """
    default = "120" if provider == "openai" else "512"
    return int(os.getenv("CALL_MAX_TOKENS", default))


def _gemini_thinking_kwargs(cls) -> dict:
    """Ask Gemini to skip hidden reasoning tokens before the first visible one.

    Non-lite Gemini flash models spend hundreds of hidden reasoning tokens
    before emitting anything, which is dead air on a phone call. Client builds
    spell the knob differently, so pick whichever name `cls` names explicitly
    and send nothing otherwise -- a wrong keyword forwarded through a **kwargs
    passthrough would fail the whole call, which is far worse than being slow.
    Set GEMINI_THINKING_BUDGET to an empty string to stop sending it.
    """
    budget = os.getenv("GEMINI_THINKING_BUDGET", "0").strip()
    if not budget:
        return {}
    accepted = inspect.signature(cls).parameters
    if "thinking_budget" in accepted:
        return {"thinking_budget": int(budget)}
    if "reasoning_effort" in accepted:
        return {"reasoning_effort": "none" if int(budget) == 0 else "low"}
    logger.info(
        "%s exposes no thinking-budget setting; Gemini may spend hidden "
        "reasoning tokens before the first spoken word",
        cls.__name__,
    )
    return {}


def _construct_logged(cls, requested: dict):
    """Instantiate `cls`, warning about any kwarg the installed build drops."""
    kwargs = _supported_kwargs(cls, **requested)
    dropped = sorted(set(requested) - set(kwargs))
    if dropped:
        logger.warning(
            "%s ignored unsupported settings: %s "
            "(the installed clawops build does not accept them)",
            cls.__name__,
            ", ".join(dropped),
        )
    return cls(**kwargs)


def _build_gemini_llm(gemini_cls):
    requested = dict(
        api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
        model=os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
        or "gemini-3.5-flash-lite",
        temperature=0.75,
        max_tokens=_call_max_tokens("gemini"),
        # Gemini returns 503 UNAVAILABLE often enough that a single try
        # loses the turn. See app/training/gemini_llm.py.
        attempts=int(os.getenv("GEMINI_ATTEMPTS", "2")),
        fallback_models=tuple(
            m.strip()
            for m in os.getenv("GEMINI_FALLBACK_MODELS", "").split(",")
            if m.strip()
        ),
        **_gemini_thinking_kwargs(gemini_cls),
    )
    return _construct_logged(gemini_cls, requested)


def _build_openai_llm(openai_cls):
    requested = dict(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        # Raised from 0.55. At the old value the caller reused the same
        # phrasing turn after turn, which reads as scripted. This matches
        # ai/llm_stream.py's default so the phone pipeline and the local
        # ai/ pipeline behave the same way.
        temperature=0.75,
        max_tokens=_call_max_tokens("openai"),
    )
    return _construct_logged(openai_cls, requested)


def _build_call_llm(gemini_cls, openai_cls):
    """Construct the in-call LLM: CALL_LLM_PROVIDER picks the primary, and the
    other provider becomes an automatic fallback for one live turn if its own
    API key is also configured.

    Without this, an exhausted or down primary key means every turn falls
    through to PhonePipelineSession's stall line -- the trainee hears "잠시만
    기다려 주십시오" instead of an answer, for the whole call. With both keys
    present, one provider failing outright still lets the call continue on
    the other. See app/training/fallback_llm.py for the switch-only-before-
    the-first-token rule that keeps this from repeating a half-spoken reply.
    """
    from app.training.fallback_llm import FallbackLLM

    if _call_llm_provider() == "gemini":
        primary, primary_label = _build_gemini_llm(gemini_cls), "Gemini"
        secondary_key, secondary_label = "OPENAI_API_KEY", "OpenAI"
        build_secondary = lambda: _build_openai_llm(openai_cls)
    else:
        primary, primary_label = _build_openai_llm(openai_cls), "OpenAI"
        secondary_key, secondary_label = "GEMINI_API_KEY", "Gemini"
        build_secondary = lambda: _build_gemini_llm(gemini_cls)

    if not os.getenv(secondary_key, "").strip():
        return primary

    return FallbackLLM(
        primary,
        build_secondary(),
        primary_label=primary_label,
        secondary_label=secondary_label,
    )


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
    provider = _call_llm_provider()
    try:
        if provider == "gemini":
            from clawops.agent import ClawOpsAgent, GeminiRealtime
        else:
            from clawops.agent import ClawOpsAgent, OpenAIRealtime
    except ImportError as exc:
        raise CallConfigurationError(
            "ClawOps Realtime dependencies are missing; rebuild the backend image "
            "with clawops[agent,openai,gemini]"
        ) from exc

    logger.warning(
        "DEEPGRAM_API_KEY or ELEVENLABS_API_KEY is missing; "
        "falling back to %s Realtime",
        provider,
    )
    if provider == "gemini":
        session = GeminiRealtime(
            api_key=os.getenv("GEMINI_API_KEY", "").strip() or None,
            system_prompt=scenario.system_prompt,
            voice=os.getenv("CLAWOPS_GEMINI_VOICE", "Kore"),
            language="ko",
        )
    else:
        session = OpenAIRealtime(
            system_prompt=scenario.system_prompt,
            voice=os.getenv("CLAWOPS_VOICE", "marin"),
            language="ko",
        )
    return ClawOpsAgent(from_=from_number, session=session)


async def start_outbound_call(session_id: str) -> str:
    agent, call_session, scenario = await _create_outbound_call(session_id)
    asyncio.create_task(_monitor_call(session_id, agent, call_session, scenario))
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
    from_number = _outbound_phone_number(session.currentTrainingType)
    _require_call_llm_key()

    try:
        # Normally instant: a fixed playbook is picked in process. The timeout
        # only bites when DYNAMIC_SCENARIO is on and an LLM is writing one.
        scenario = await asyncio.wait_for(
            get_runtime_scenario(),
            timeout=float(os.getenv("SCENARIO_GENERATION_TIMEOUT_SEC", "20")),
        )
    except Exception:
        logger.exception("Scenario selection failed; using the default scenario")
        try:
            from app.training.scenarios import get_call_scenario

            scenario = get_call_scenario()
        except Exception as fallback_exc:
            raise CallConfigurationError(str(fallback_exc)) from fallback_exc

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
    return agent, call_session, scenario


async def _monitor_call(session_id: str, agent, call_session, scenario=None) -> None:
    try:
        await call_session.wait()
        if trainee_spoke(session_id, call_session.call_id):
            update_call_status(session_id, "completed")
            _complete_call(call_session.call_id)
            _complete_scheduled_training(session_id)
            try:
                await asyncio.wait_for(
                    build_draft_report(session_id, scenario=scenario), timeout=20
                )
            except Exception:
                logger.exception("Draft report failed: session_id=%s", session_id)
            else:
                try:
                    from app.services.training_scheduler import (
                        schedule_unannounced_training,
                    )

                    schedule_unannounced_training(session_id)
                except Exception:
                    logger.exception(
                        "Unannounced training scheduling failed: session_id=%s",
                        session_id,
                    )
            try:
                await request_clawops_transcript(call_session.call_id)
            except Exception:
                logger.exception(
                    "ClawOps transcript request failed: session_id=%s",
                    session_id,
                )
        elif call_had_transcript(session_id, call_session.call_id):
            update_call_status(session_id, "silent")
            _complete_call(call_session.call_id)
            update_report_status(session_id, "none")
            _retry_scheduled_training(session_id, "no_trainee_speech")
            logger.info(
                "Training call connected without trainee speech session=%s",
                session_id,
            )
        else:
            update_call_status(session_id, "missed")
            _fail_call(call_session.call_id, "no_answer")
            update_report_status(session_id, "none")
            _retry_scheduled_training(session_id, "call_not_connected")
            logger.info("Training call missed session=%s", session_id)
    except Exception:
        update_call_status(session_id, "failed")
        _fail_call(call_session.call_id, "ClawOps call failed")
        _retry_scheduled_training(session_id, "call_monitor_failed")
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
        _retry_scheduled_training(session_id, "outbound_lock_timeout")
        logger.warning(
            "Previous ClawOps agent still running; skip session=%s", session_id
        )
        return
    try:
        asyncio.run(_start_and_monitor_call(session_id))
    except Exception:
        update_call_status(session_id, "failed")
        _retry_scheduled_training(session_id, "call_start_failed")
        logger.exception("Failed to start ClawOps call: session_id=%s", session_id)
    finally:
        _outbound_lock.release()


async def _start_and_monitor_call(session_id: str) -> None:
    agent, call_session, scenario = await _create_outbound_call(session_id)
    await _monitor_call(session_id, agent, call_session, scenario)


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


def _outbound_phone_number(training_type: str) -> str:
    if training_type == "unannounced":
        return _require_env("CLAWOPS_UNANNOUNCED_PHONE_NUMBER")
    return _require_env("CLAWOPS_PHONE_NUMBER")


def _complete_scheduled_training(session_id: str) -> None:
    from app.services.training_scheduler import complete_unannounced_training

    complete_unannounced_training(session_id)


def _retry_scheduled_training(session_id: str, reason: str) -> None:
    from app.services.training_scheduler import retry_unannounced_training

    retry_unannounced_training(session_id, reason)
