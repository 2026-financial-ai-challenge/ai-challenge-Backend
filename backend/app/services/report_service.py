from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from app.schemas.report import (
    BehaviorItem,
    GetReportResponse,
    TrainingReport,
    TranscriptTurn,
)
from app.services.session_service import set_session_call_id, update_report_status
from app.training.scenarios import ensure_ai_importable


logger = logging.getLogger(__name__)

Role = Literal["user", "assistant"]
ReportStatus = Literal["none", "pending", "draft", "final", "failed"]

_HANG_UP = re.compile(
    r"(끊겠|끊을게|끊을게요|끊습니다|전화 끊|나중에 걸|그만하세요|그만 전화)"
)
_SUSPECT = re.compile(
    r"(의심|누구세요|어디(?:세요|죠)|금감원|검찰|경찰|사기|피싱|대표번호|확인(해|할)|가짜)"
)
_NAME_OFFER = re.compile(
    r"(제\s*이름|성함|이름은|저는\s*[가-힣]{2,4}|[가-힣]{2,4}\s*(입니다|인데요|이라고))"
)


class _LlmReport(BaseModel):
    suspected: bool = False
    gaveName: bool = False
    triedHangup: bool = False
    summary: str = ""
    coaching: str = ""
    riskBehaviors: list[BehaviorItem] = Field(default_factory=list)
    defenseBehaviors: list[BehaviorItem] = Field(default_factory=list)


@dataclass
class _SessionReport:
    session_id: str
    call_id: str | None = None
    status: ReportStatus = "none"
    turns: list[TranscriptTurn] = field(default_factory=list)
    draft: TrainingReport | None = None
    final: TrainingReport | None = None
    clawops_summary: dict[str, Any] | None = None


_reports: dict[str, _SessionReport] = {}
_call_to_session: dict[str, str] = {}
_lock = Lock()


def reset_reports() -> None:
    with _lock:
        _reports.clear()
        _call_to_session.clear()


def session_id_for_call(call_id: str) -> str | None:
    with _lock:
        return _call_to_session.get(call_id)


def bind_call(session_id: str, call_id: str) -> None:
    with _lock:
        record = _ensure_locked(session_id)
        record.call_id = call_id
        if record.status == "none":
            record.status = "pending"
        _call_to_session[call_id] = session_id
    set_session_call_id(session_id, call_id)
    update_report_status(session_id, "pending")


def append_turn(
    session_id: str,
    role: Role,
    text: str,
    *,
    call_id: str | None = None,
) -> None:
    cleaned = (text or "").strip()
    if role not in {"user", "assistant"} or not cleaned:
        return
    with _lock:
        record = _ensure_locked(session_id)
        if call_id:
            record.call_id = call_id
            _call_to_session[call_id] = session_id
        if record.status == "none":
            record.status = "pending"
        record.turns.append(TranscriptTurn(role=role, text=cleaned))
    if call_id:
        set_session_call_id(session_id, call_id)


def get_report(session_id: str) -> GetReportResponse:
    with _lock:
        record = _reports.get(session_id)
        if record is None:
            return GetReportResponse(sessionId=session_id, status="none")
        return GetReportResponse(
            sessionId=record.session_id,
            callId=record.call_id,
            status=record.status,
            turns=list(record.turns),
            draft=record.draft,
            final=record.final,
            clawopsSummary=record.clawops_summary,
        )


def format_live_turns(turns: list[TranscriptTurn]) -> str:
    lines: list[str] = []
    for turn in turns:
        speaker = "훈련자" if turn.role == "user" else "상대"
        text = turn.text.strip()
        if text:
            lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def format_clawops_segments(segments: Any) -> str:
    lines: list[str] = []
    for segment in segments or []:
        speaker_raw = _attr(segment, "speaker")
        text = str(_attr(segment, "text") or "").strip()
        if not text:
            continue
        speaker = "훈련자" if speaker_raw == "CUSTOMER" else "상대"
        lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def heuristic_report(transcript: str, *, source: Literal["live", "clawops"]) -> TrainingReport:
    user_text = "\n".join(
        line.split("]", 1)[-1].strip()
        for line in (transcript or "").splitlines()
        if line.startswith("[훈련자]")
    )
    blob = user_text or transcript or ""
    empty = not blob.strip()
    return TrainingReport(
        suspected=bool(_SUSPECT.search(blob)),
        gaveName=bool(_NAME_OFFER.search(blob)),
        triedHangup=bool(_HANG_UP.search(blob)),
        summary=(
            "통화 내용이 거의 없어 바로 평가하기 어렵습니다."
            if empty
            else "실시간 받아쓰기를 바탕으로 한 빠른 회고입니다. 최종 전사가 오면 다시 정리합니다."
        ),
        coaching=(
            "상대가 누구인지 확인하고, 성함 같은 개인정보를 대지 말고, "
            "의심되면 바로 끊으세요."
        ),
        source=source,
    )


async def score_conversation(
    transcript: str,
    *,
    source: Literal["live", "clawops"],
    clawops_summary: dict[str, Any] | None = None,
    client: Any | None = None,
) -> TrainingReport:
    if not (transcript or "").strip():
        return heuristic_report(transcript, source=source)

    try:
        parsed = await _ask_report_llm(
            transcript,
            source=source,
            clawops_summary=clawops_summary,
            client=client,
        )
    except Exception:
        logger.exception("Training report LLM failed; using heuristic")
        return heuristic_report(transcript, source=source)

    risk_labels, defense_labels = _behavior_labels()
    return TrainingReport(
        suspected=parsed.suspected,
        gaveName=parsed.gaveName,
        triedHangup=parsed.triedHangup,
        summary=parsed.summary.strip() or heuristic_report(transcript, source=source).summary,
        coaching=parsed.coaching.strip()
        or heuristic_report(transcript, source=source).coaching,
        riskBehaviors=_keep_known(parsed.riskBehaviors, risk_labels),
        defenseBehaviors=_keep_known(parsed.defenseBehaviors, defense_labels),
        source=source,
    )


async def build_draft_report(session_id: str, *, client: Any | None = None) -> TrainingReport:
    with _lock:
        record = _ensure_locked(session_id)
        turns = list(record.turns)
    transcript = format_live_turns(turns)
    report = await score_conversation(transcript, source="live", client=client)
    with _lock:
        record = _ensure_locked(session_id)
        record.draft = report
        if record.status != "final":
            record.status = "draft"
        status = record.status
    update_report_status(session_id, status)
    logger.info("Draft report ready session=%s turns=%s", session_id, len(turns))
    return report


async def request_clawops_transcript(call_id: str) -> None:
    try:
        calls = _clawops_calls()
    except Exception:
        logger.exception("ClawOps client unavailable; skip transcript request")
        return
    try:
        await asyncio.to_thread(calls.request_transcript, call_id)
        logger.info("Requested ClawOps transcript call_id=%s", call_id)
    except Exception as exc:
        name = type(exc).__name__
        if name in {"ConflictError", "BadRequestError"}:
            logger.info(
                "ClawOps transcript not requested (%s): call_id=%s %s",
                name,
                call_id,
                exc,
            )
            return
        logger.exception("ClawOps transcript request failed: call_id=%s", call_id)


async def handle_transcript_event(params: dict[str, str]) -> None:
    call_id = params.get("CallId", "")
    event = params.get("Event", "")
    session_id = session_id_for_call(call_id)
    if session_id is None:
        logger.info("No training session for ClawOps call_id=%s", call_id)
        return
    if event == "transcript.failed":
        logger.warning(
            "ClawOps transcript failed session=%s call_id=%s stage=%s error=%s",
            session_id,
            call_id,
            params.get("Stage", ""),
            params.get("ErrorMessage", ""),
        )
        return
    if event == "transcript.completed":
        await build_final_report(session_id, call_id)


async def build_final_report(
    session_id: str,
    call_id: str,
    *,
    client: Any | None = None,
) -> TrainingReport | None:
    transcript_status = await asyncio.to_thread(fetch_clawops_transcript, call_id)
    segments = getattr(transcript_status, "segments", None) if transcript_status else None
    status_name = getattr(transcript_status, "status", None) if transcript_status else None
    if status_name != "completed" or not segments:
        logger.warning(
            "ClawOps transcript not ready session=%s call_id=%s status=%s",
            session_id,
            call_id,
            status_name,
        )
        return None

    transcript = format_clawops_segments(segments)
    summary = await asyncio.to_thread(fetch_clawops_summary, call_id)
    report = await score_conversation(
        transcript,
        source="clawops",
        clawops_summary=summary,
        client=client,
    )
    with _lock:
        record = _ensure_locked(session_id)
        record.call_id = call_id
        record.final = report
        record.clawops_summary = summary
        record.status = "final"
    update_report_status(session_id, "final")
    logger.info("Final report ready session=%s call_id=%s", session_id, call_id)
    return report


def fetch_clawops_transcript(call_id: str) -> Any:
    return _clawops_calls().get_transcript(call_id)


def fetch_clawops_summary(call_id: str) -> dict[str, Any] | None:
    try:
        status = _clawops_calls().get_summary(call_id)
    except Exception:
        logger.exception("ClawOps summary fetch failed: call_id=%s", call_id)
        return None
    if getattr(status, "status", None) != "completed":
        return None
    result = getattr(status, "result_json", None)
    return dict(result) if isinstance(result, dict) else None


def register_transcript_listener(agent: Any, session_id: str) -> None:
    async def on_transcript(call: Any, role: str, text: str) -> None:
        try:
            append_turn(
                session_id,
                role,  # type: ignore[arg-type]
                text,
                call_id=getattr(call, "call_id", None),
            )
        except Exception:
            logger.exception("Failed to store live turn session=%s", session_id)

    agent.on("transcript")(on_transcript)


def _ensure_locked(session_id: str) -> _SessionReport:
    record = _reports.get(session_id)
    if record is None:
        record = _SessionReport(session_id=session_id)
        _reports[session_id] = record
    return record


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name) or obj.get(_to_camel(name))
    return getattr(obj, name, None)


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _keep_known(items: list[BehaviorItem], allowed: tuple[str, ...]) -> list[BehaviorItem]:
    return [item for item in items if item.label in allowed and item.evidence.strip()]


def _behavior_labels() -> tuple[tuple[str, ...], tuple[str, ...]]:
    ensure_ai_importable()
    from ai.classifier import DEFENSE_LABELS, RISK_LABELS

    return RISK_LABELS, DEFENSE_LABELS


def _clawops_calls() -> Any:
    from clawops import ClawOps

    api_key = os.getenv("CLAWOPS_API_KEY", "").strip()
    account_id = os.getenv("CLAWOPS_ACCOUNT_ID", "").strip()
    if not api_key or not account_id:
        raise RuntimeError("CLAWOPS_API_KEY or CLAWOPS_ACCOUNT_ID is not set")
    return ClawOps(api_key=api_key, account_id=account_id).calls


async def _ask_report_llm(
    transcript: str,
    *,
    source: Literal["live", "clawops"],
    clawops_summary: dict[str, Any] | None,
    client: Any | None,
) -> _LlmReport:
    from openai import AsyncOpenAI

    risk_labels, defense_labels = _behavior_labels()
    openai_client = client or AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    source_note = (
        "실시간 STT라 오인식이 있을 수 있다. 확실한 것만 표시한다."
        if source == "live"
        else "녹음 기준 화자 분리 전사다. 이 전사를 우선한다."
    )
    summary_note = ""
    if clawops_summary:
        summary_note = (
            "\n\nClawOps 일반 요약(초안, 교육 루브릭 아님):\n"
            + json.dumps(clawops_summary, ensure_ascii=False)
        )
    system = f"""
너는 보이스피싱 모의훈련 코치다. 이 대화는 사전 동의 하의 교육 시뮬레이션이다.
실제 금감원·검찰·은행 전화가 아니다. 점수나 등급은 매기지 마라.
{source_note}

반드시 JSON 객체만 반환한다.
형식:
{{
  "suspected": true,
  "gaveName": false,
  "triedHangup": true,
  "summary": "2~4문장 한국어 요약",
  "coaching": "다음에 이렇게 하세요. 2~3문장",
  "riskBehaviors": [{{"label": "...", "evidence": "..."}}],
  "defenseBehaviors": [{{"label": "...", "evidence": "..."}}]
}}

규칙:
- suspected: 훈련자가 상대 신원·기관을 의심하거나 확인하려 했는지
- gaveName: 훈련자가 성함이나 이름을 댔는지
- triedHangup: 끊겠다고 하거나 통화를 끝내려고 했는지
- riskBehaviors label은 다음만: {", ".join(risk_labels)}
- defenseBehaviors label은 다음만: {", ".join(defense_labels)}
- evidence는 짧은 인용. 근거 없으면 그 라벨을 넣지 마라
- 추측으로 채우지 마라
""".strip()

    response = await openai_client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"대화 기록:\n{transcript}{summary_note}",
            },
        ],
    )
    raw = response.choices[0].message.content or "{}"
    try:
        return _LlmReport.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"Report LLM returned invalid JSON: {raw[:500]}") from exc
