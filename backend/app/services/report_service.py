from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import delete, func, select

from app.database import SessionLocal
from app.models.call import Call
from app.models.training_report import TrainingReportRecord
from app.models.transcript_turn import TranscriptTurnRecord

from app.schemas.report import (
    BehaviorItem,
    GetReportResponse,
    TrainingReport,
    TranscriptTurn,
)
from app.services.session_service import set_session_call_id, update_report_status
from app.training.scenarios import ensure_ai_importable, get_call_scenario


logger = logging.getLogger(__name__)

Role = Literal["user", "assistant"]
ReportStatus = Literal["none", "pending", "draft", "final", "failed"]

BASE_RESPONSE_SCORE = 60
RISK_SCORE_WEIGHTS = {
    "개인정보 제공": -15,
    "금융정보 제공": -25,
    "상대방 기관명 신뢰": -10,
    "송금 의사 표현": -20,
    "링크 접근 의사": -15,
    "앱 설치 의사": -20,
    "통화 장시간 지속": -10,
}
DEFENSE_SCORE_WEIGHTS = {
    "상대방 신원 확인": 8,
    "공식 대표번호 확인 의사": 15,
    "개인정보 제공 거절": 15,
    "송금 거절": 20,
    "전화 종료(빠른 판단)": 15,
    "신고 의사 표현": 12,
}

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


def reset_reports() -> None:
    with SessionLocal.begin() as db:
        db.execute(delete(TrainingReportRecord))
        db.execute(delete(TranscriptTurnRecord))


def session_id_for_call(call_id: str) -> str | None:
    with SessionLocal() as db:
        return db.scalar(select(Call.session_id).where(Call.clawops_call_id == call_id))


def bind_call(session_id: str, call_id: str) -> None:
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
    if call_id:
        set_session_call_id(session_id, call_id)
    with SessionLocal.begin() as db:
        call = _get_call(db, call_id) if call_id else _latest_call(db, session_id)
        sequence = db.scalar(
            select(func.coalesce(func.max(TranscriptTurnRecord.sequence), 0)).where(
                TranscriptTurnRecord.session_id == session_id,
                TranscriptTurnRecord.source == "live",
            )
        )
        db.add(
            TranscriptTurnRecord(
                session_id=session_id,
                call_id=call.id if call is not None else None,
                role=role,
                text=cleaned,
                source="live",
                sequence=int(sequence or 0) + 1,
            )
        )
    update_report_status(session_id, "pending")


def get_report(session_id: str) -> GetReportResponse:
    with SessionLocal() as db:
        call = _latest_call(db, session_id)
        turns = db.scalars(
            select(TranscriptTurnRecord)
            .where(
                TranscriptTurnRecord.session_id == session_id,
                TranscriptTurnRecord.source == "live",
            )
            .order_by(TranscriptTurnRecord.sequence)
        ).all()
        reports = db.scalars(
            select(TrainingReportRecord).where(
                TrainingReportRecord.session_id == session_id
            )
        ).all()
        draft_row = next((row for row in reports if row.source == "live"), None)
        final_row = next((row for row in reports if row.source == "clawops"), None)
        status: ReportStatus = (
            "final"
            if final_row is not None
            else "draft"
            if draft_row is not None
            else "pending"
            if call is not None or turns
            else "none"
        )
        return GetReportResponse(
            sessionId=session_id,
            callId=call.clawops_call_id if call is not None else None,
            status=status,
            turns=[TranscriptTurn(role=row.role, text=row.text) for row in turns],
            draft=_report_schema(draft_row),
            final=_report_schema(final_row),
            clawopsSummary=final_row.clawops_summary if final_row is not None else None,
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
    suspected = bool(_SUSPECT.search(blob))
    gave_name = bool(_NAME_OFFER.search(blob))
    tried_hangup = bool(_HANG_UP.search(blob))
    fallback_score = max(
        0,
        min(
            100,
            BASE_RESPONSE_SCORE
            + (8 if suspected else 0)
            - (15 if gave_name else 0)
            + (15 if tried_hangup else 0),
        ),
    )
    return TrainingReport(
        score=fallback_score,
        suspected=suspected,
        gaveName=gave_name,
        triedHangup=tried_hangup,
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
    risk_behaviors = _keep_known(parsed.riskBehaviors, risk_labels)
    defense_behaviors = _keep_known(parsed.defenseBehaviors, defense_labels)
    return TrainingReport(
        score=calculate_response_score(risk_behaviors, defense_behaviors),
        suspected=parsed.suspected,
        gaveName=parsed.gaveName,
        triedHangup=parsed.triedHangup,
        summary=parsed.summary.strip() or heuristic_report(transcript, source=source).summary,
        coaching=parsed.coaching.strip()
        or heuristic_report(transcript, source=source).coaching,
        riskBehaviors=risk_behaviors,
        defenseBehaviors=defense_behaviors,
        source=source,
    )


async def build_draft_report(session_id: str, *, client: Any | None = None) -> TrainingReport:
    turns = get_report(session_id).turns
    transcript = format_live_turns(turns)
    report = await score_conversation(transcript, source="live", client=client)
    _save_report(session_id, report, status="draft")
    status: ReportStatus = "final" if get_report(session_id).final is not None else "draft"
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
    _replace_clawops_turns(session_id, call_id, segments)
    _save_report(
        session_id,
        report,
        status="final",
        call_id=call_id,
        clawops_summary=summary,
    )
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


def _latest_call(db: Any, session_id: str) -> Call | None:
    return db.scalar(
        select(Call)
        .where(Call.session_id == session_id)
        .order_by(Call.created_at.desc(), Call.id.desc())
        .limit(1)
    )


def _get_call(db: Any, clawops_call_id: str | None) -> Call | None:
    if not clawops_call_id:
        return None
    return db.scalar(select(Call).where(Call.clawops_call_id == clawops_call_id))


def _report_schema(row: TrainingReportRecord | None) -> TrainingReport | None:
    if row is None:
        return None
    return TrainingReport(
        score=row.score,
        suspected=row.suspected,
        gaveName=row.gave_name,
        triedHangup=row.tried_hangup,
        summary=row.summary,
        coaching=row.coaching,
        riskBehaviors=[BehaviorItem.model_validate(item) for item in row.risk_behaviors],
        defenseBehaviors=[
            BehaviorItem.model_validate(item) for item in row.defense_behaviors
        ],
        source=row.source,
    )


def _save_report(
    session_id: str,
    report: TrainingReport,
    *,
    status: Literal["draft", "final"],
    call_id: str | None = None,
    clawops_summary: dict[str, Any] | None = None,
) -> None:
    with SessionLocal.begin() as db:
        call = _get_call(db, call_id) if call_id else _latest_call(db, session_id)
        row = db.scalar(
            select(TrainingReportRecord).where(
                TrainingReportRecord.session_id == session_id,
                TrainingReportRecord.source == report.source,
            )
        )
        values = {
            "call_id": call.id if call is not None else None,
            "status": status,
            "score": report.score,
            "suspected": report.suspected,
            "gave_name": report.gaveName,
            "tried_hangup": report.triedHangup,
            "summary": report.summary,
            "coaching": report.coaching,
            "risk_behaviors": [item.model_dump() for item in report.riskBehaviors],
            "defense_behaviors": [
                item.model_dump() for item in report.defenseBehaviors
            ],
            "clawops_summary": clawops_summary,
        }
        if row is None:
            db.add(
                TrainingReportRecord(
                    session_id=session_id,
                    source=report.source,
                    **values,
                )
            )
        else:
            for key, value in values.items():
                setattr(row, key, value)


def _replace_clawops_turns(session_id: str, call_id: str, segments: Any) -> None:
    with SessionLocal.begin() as db:
        call = _get_call(db, call_id)
        db.execute(
            delete(TranscriptTurnRecord).where(
                TranscriptTurnRecord.session_id == session_id,
                TranscriptTurnRecord.source == "clawops",
            )
        )
        sequence = 0
        for segment in segments or []:
            text = str(_attr(segment, "text") or "").strip()
            if not text:
                continue
            sequence += 1
            db.add(
                TranscriptTurnRecord(
                    session_id=session_id,
                    call_id=call.id if call is not None else None,
                    role="user" if _attr(segment, "speaker") == "CUSTOMER" else "assistant",
                    text=text,
                    source="clawops",
                    sequence=sequence,
                )
            )


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name) or obj.get(_to_camel(name))
    return getattr(obj, name, None)


def _to_camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _keep_known(items: list[BehaviorItem], allowed: tuple[str, ...]) -> list[BehaviorItem]:
    return [item for item in items if item.label in allowed and item.evidence.strip()]


def calculate_response_score(
    risk_behaviors: list[BehaviorItem],
    defense_behaviors: list[BehaviorItem],
) -> int:
    score = BASE_RESPONSE_SCORE
    risk_labels = {item.label for item in risk_behaviors}
    defense_labels = {item.label for item in defense_behaviors}
    score += sum(RISK_SCORE_WEIGHTS.get(label, 0) for label in risk_labels)
    score += sum(
        DEFENSE_SCORE_WEIGHTS.get(label, 0) for label in defense_labels
    )
    return max(0, min(100, score))


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
    scenario_note = _scenario_report_note()
    system = f"""
너는 보이스피싱 모의훈련 코치다. 이 대화는 사전 동의 하의 교육 시뮬레이션이다.
실제 금감원·검찰·은행 전화가 아니다. 점수나 등급은 매기지 마라.
{source_note}
{scenario_note}

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


def _scenario_report_note() -> str:
    try:
        scenario = get_call_scenario()
    except Exception:
        logger.exception("Could not load scenario rubric for report")
        return ""

    tactics = getattr(scenario, "tactics", ())
    red_flags = getattr(scenario, "red_flags", ())
    ideal_response = getattr(scenario, "ideal_trainee_response", None)
    if not (tactics or red_flags or ideal_response):
        return ""

    lines = ["[이번 훈련 시나리오 평가 기준]"]
    if tactics:
        lines.append("사용된 심리 기법: " + ", ".join(tactics))
    if red_flags:
        lines.append("알아챘어야 할 위험 신호:")
        lines.extend(f"- {flag}" for flag in red_flags)
    if ideal_response:
        lines.append(f"권장 대응: {ideal_response}")
    lines.append(
        "위 기준은 summary와 coaching 작성에 활용하되, 실제 대화에서 관찰되지 않은 "
        "행동을 riskBehaviors나 defenseBehaviors에 추가하지 마라."
    )
    return "\n".join(lines)
