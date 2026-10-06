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
from app.models.scheduled_training import ScheduledTraining
from app.models.training_report import TrainingReportRecord
from app.models.transcript_turn import TranscriptTurnRecord
from app.models.web_training import WebTrainingEvent

from app.schemas.report import (
    BehaviorItem,
    GetReportResponse,
    TrainingReport,
    TranscriptTurn,
    WebTrainingReport,
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

# 웹 훈련 이벤트를 전화 훈련과 같은 행동 라벨로 환산한다. 전화에서는 "의사"만
# 확인되지만 웹에서는 실제 행동이 남으므로, 같은 라벨·같은 가중치를 그대로 쓰되
# 증거 문구만 웹 맥락으로 표기한다. 매핑이 없는 이벤트는 점수에 반영하지 않는다.
# 결과는 webTraining으로 따로 내고, 리포트 화면이 전화 점수와 나란히 비교한다.
WEB_EVENT_LABELS: dict[str, str] = {
    # 문자 링크를 연 것 자체가 사기범이 노리는 첫 행동이다.
    "link_opened": "링크 접근 의사",
    "identity_submitted": "개인정보 제공",
    "case_lookup_submitted": "개인정보 제공",
    "financial_info_submitted": "금융정보 제공",
    "app_install_clicked": "앱 설치 의사",
    "report_clicked": "신고 의사 표현",
    "left_without_input": "전화 종료(빠른 판단)",
}
WEB_EVENT_EVIDENCE: dict[str, str] = {
    "link_opened": "웹 훈련: 문자 링크 열람",
    "identity_submitted": "웹 훈련: 본인인증 정보 제출",
    "case_lookup_submitted": "웹 훈련: 성명으로 사건 조회 시도",
    "financial_info_submitted": "웹 훈련: 금융정보 입력 제출",
    "app_install_clicked": "웹 훈련: 안내 앱 설치 버튼 클릭",
    "report_clicked": "웹 훈련: 의심/신고 버튼 클릭",
    "left_without_input": "웹 훈련: 정보 입력 없이 이탈",
}

def _trainee_tried_hangup(trainee_text: str) -> bool:
    """Shared with the live pipeline so the caller's behaviour and the score
    agree on what "the trainee tried to end the call" means.

    Asked one utterance at a time rather than over the whole blob: the rule
    is about where in an utterance a closing lands, and joining every turn
    into one string would put each of them in the middle of it.
    """
    ensure_ai_importable()
    from ai.hangup import wants_hang_up

    return any(wants_hang_up(line) for line in (trainee_text or "").splitlines())
_SUSPECT = re.compile(
    r"(의심|누구세요|어디(?:세요|죠)|금감원|검찰|경찰|사기|피싱|대표번호|확인(해|할)|가짜)"
)
_NAME_OFFER = re.compile(
    r"(제\s*이름|성함|이름은|저는\s*[가-힣]{2,4}|[가-힣]{2,4}\s*(입니다|인데요|이라고))"
)


class _LlmReport(BaseModel):
    suspected: bool = False
    suspectedEvidence: str = ""
    gaveName: bool = False
    gaveNameEvidence: str = ""
    triedHangup: bool = False
    triedHangupEvidence: str = ""
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


def _scenario_for_call(call_id: str):
    """The scenario this call actually ran, for scoring and coaching.

    Calls placed before scenario_id was recorded have none; get_call_scenario()
    then picks the default, which is what the whole report used to be scored
    against no matter which scam the trainee actually heard.
    """
    with SessionLocal() as db:
        scenario_id = db.scalar(
            select(Call.scenario_id).where(Call.clawops_call_id == call_id)
        )
    ensure_ai_importable()
    if not scenario_id:
        return get_call_scenario()
    from ai.scenarios import get_scenario

    return get_scenario(scenario_id)


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


# Unannounced jobs that will never produce a result.
_ABANDONED_JOB_STATUSES = ("failed", "cancelled")


def get_report(session_id: str) -> GetReportResponse:
    with SessionLocal() as db:
        call = _latest_call(db, session_id)
        reports = db.scalars(
            select(TrainingReportRecord).where(
                TrainingReportRecord.session_id == session_id
            )
        ).all()
        live_row = next((row for row in reports if row.source == "live"), None)
        clawops_row = next((row for row in reports if row.source == "clawops"), None)
        # The announced call's own result: the recording-based report when there
        # is one, else the live draft. Managed calls never write a live report, so
        # reading only "live" here hid the whole first call.
        announced_row = clawops_row or live_row
        announced_turn_rows = _turn_rows(
            db, session_id, "clawops" if clawops_row is not None else "live"
        ) or _turn_rows(db, session_id, "live")
        turns = announced_turn_rows
        unannounced_row = None
        unannounced_turn_rows: list[TranscriptTurnRecord] = []
        # Each round shows three reports: the first call, the unannounced call,
        # and the comparison of the two. The comparison only exists when both
        # calls produced a report; a missing one is shown as "not created".
        final_report: TrainingReport | None = None

        # The unannounced call runs in its own session so retries and call records
        # stay independent. For the user-facing report, however, it is the final
        # result of the original announced-training session.
        # A job that gave up (failed/cancelled) has no unannounced result to wait
        # for, so the round ends with the announced call alone.
        scheduled = db.scalar(
            select(ScheduledTraining).where(
                ScheduledTraining.source_session_id == session_id,
                ScheduledTraining.status.not_in(_ABANDONED_JOB_STATUSES),
            )
        )
        if scheduled is None:
            # A live draft alone still waits for the recording-based report.
            round_done = clawops_row is not None
        else:
            if scheduled.result_session_id:
                result_reports = db.scalars(
                    select(TrainingReportRecord).where(
                        TrainingReportRecord.session_id
                        == scheduled.result_session_id
                    )
                ).all()
                unannounced_row = next(
                    (row for row in result_reports if row.source == "clawops"),
                    None,
                ) or next(
                    (row for row in result_reports if row.source == "live"),
                    None,
                )
                if unannounced_row is not None:
                    result_call = _latest_call(db, scheduled.result_session_id)
                    unannounced_turn_rows = _turn_rows(
                        db, scheduled.result_session_id, unannounced_row.source
                    )
                    call = result_call or call
                    turns = unannounced_turn_rows
                    final_report = _combine_reports(
                        _report_schema(announced_row),
                        _report_schema(unannounced_row),
                    )
            round_done = unannounced_row is not None or scheduled.status == "completed"
        web_event_types = db.scalars(
            select(WebTrainingEvent.event_type)
            .where(WebTrainingEvent.session_id == session_id)
            .order_by(WebTrainingEvent.created_at)
        ).all()
        web_training = (
            score_web_events(list(web_event_types)) if web_event_types else None
        )

        status: ReportStatus = (
            "final"
            if round_done
            else "draft"
            if announced_row is not None
            else "pending"
            if call is not None or turns
            else "none"
        )
        return GetReportResponse(
            sessionId=session_id,
            callId=call.clawops_call_id if call is not None else None,
            status=status,
            turns=[TranscriptTurn(role=row.role, text=row.text) for row in turns],
            draftTurns=_turn_schemas(announced_turn_rows),
            unannouncedTurns=_turn_schemas(unannounced_turn_rows),
            draft=_report_schema(announced_row),
            unannounced=_report_schema(unannounced_row),
            final=final_report,
            clawopsSummary=(unannounced_row or announced_row).clawops_summary
            if (unannounced_row or announced_row) is not None
            else None,
            webTraining=web_training,
        )


def _turn_rows(db, session_id: str, source: str) -> list[TranscriptTurnRecord]:
    return list(
        db.scalars(
            select(TranscriptTurnRecord)
            .where(
                TranscriptTurnRecord.session_id == session_id,
                TranscriptTurnRecord.source == source,
            )
            .order_by(TranscriptTurnRecord.sequence)
        ).all()
    )


def format_live_turns(turns: list[TranscriptTurn]) -> str:
    lines: list[str] = []
    for turn in turns:
        speaker = "훈련자" if turn.role == "user" else "상대"
        text = turn.text.strip()
        if text:
            lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def format_clawops_segments(segments: Any, agent_speaker: str | None = None) -> str:
    lines: list[str] = []
    for segment in segments or []:
        text = str(_attr(segment, "text") or "").strip()
        if not text:
            continue
        speaker = "훈련자" if _is_trainee(segment, agent_speaker) else "상대"
        lines.append(f"[{speaker}] {text}")
    return "\n".join(lines)


def _is_trainee(segment: Any, agent_speaker: str | None) -> bool:
    """Transcripts since 2026-08 label speakers speaker_0/1/... with no fixed
    mapping to roles, so the AI's speaker is worked out from what it said
    (agent_speaker_for). Without one, fall back to the old AGENT/CUSTOMER
    labels."""
    speaker = _attr(segment, "speaker")
    if agent_speaker is None:
        return speaker == "CUSTOMER"
    return speaker != agent_speaker


def agent_speaker_for(segments: Any, scenario: Any) -> str | None:
    ensure_ai_importable()
    from ai.transcript import identify_agent_speaker

    return identify_agent_speaker(segments or [], scenario)


def _audit_agent_speech(call_id: str, segments: Any, agent_speaker: str | None) -> None:
    """Managed agents speak straight from ClawOps, so nothing can block a bad
    line mid-call. Check the transcript afterwards and log every slip so the
    shared instructions can be fixed."""
    if agent_speaker is None:
        return
    ensure_ai_importable()
    from ai.harness import audit_transcript

    agent_texts = [
        str(_attr(s, "text") or "")
        for s in segments or []
        if _attr(s, "speaker") == agent_speaker
    ]
    for finding in audit_transcript(agent_texts):
        logger.warning(
            "Safety audit call_id=%s kind=%s text=%s",
            call_id,
            finding["kind"],
            finding["text"],
        )


def heuristic_report(transcript: str, *, source: Literal["live", "clawops"]) -> TrainingReport:
    blob = _trainee_text(transcript)
    empty = not blob.strip()
    suspected = bool(_SUSPECT.search(blob))
    gave_name = bool(_NAME_OFFER.search(blob))
    tried_hangup = _trainee_tried_hangup(blob)
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
    scenario: Any | None = None,
) -> TrainingReport:
    trainee_text = _trainee_text(transcript)
    if not trainee_text:
        return heuristic_report(transcript, source=source)

    try:
        parsed = await _ask_report_llm(
            transcript,
            source=source,
            clawops_summary=clawops_summary,
            client=client,
            scenario=scenario,
        )
    except Exception:
        logger.exception("Training report LLM failed; using heuristic")
        return heuristic_report(transcript, source=source)

    risk_labels, defense_labels = _behavior_labels()
    risk_behaviors = _keep_supported(
        parsed.riskBehaviors,
        risk_labels,
        trainee_text,
    )
    defense_behaviors = _keep_supported(
        parsed.defenseBehaviors,
        defense_labels,
        trainee_text,
    )
    suspected = _grounded_flag(parsed.suspected, parsed.suspectedEvidence, trainee_text)
    gave_name = _grounded_flag(parsed.gaveName, parsed.gaveNameEvidence, trainee_text)
    tried_hangup = _grounded_flag(parsed.triedHangup, parsed.triedHangupEvidence, trainee_text)
    return TrainingReport(
        score=calculate_response_score(risk_behaviors, defense_behaviors),
        suspected=suspected,
        gaveName=gave_name,
        triedHangup=tried_hangup,
        summary=parsed.summary.strip() or heuristic_report(transcript, source=source).summary,
        coaching=parsed.coaching.strip()
        or heuristic_report(transcript, source=source).coaching,
        riskBehaviors=risk_behaviors,
        defenseBehaviors=defense_behaviors,
        source=source,
    )


async def build_draft_report(
    session_id: str,
    *,
    client: Any | None = None,
    scenario: Any | None = None,
) -> TrainingReport:
    turns = get_report(session_id).turns
    transcript = format_live_turns(turns)
    report = await score_conversation(
        transcript, source="live", client=client, scenario=scenario
    )
    _save_report(session_id, report, status="draft")
    status: ReportStatus = "final" if get_report(session_id).status == "final" else "draft"
    update_report_status(session_id, status)
    _mark_source_report_ready(session_id)
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

    scenario = _scenario_for_call(call_id)
    agent_speaker = agent_speaker_for(segments, scenario)
    if agent_speaker is None:
        logger.warning(
            "Could not tell the AI speaker apart call_id=%s; using CUSTOMER labels",
            call_id,
        )
    _audit_agent_speech(call_id, segments, agent_speaker)
    transcript = format_clawops_segments(segments, agent_speaker)
    summary = await asyncio.to_thread(fetch_clawops_summary, call_id)
    report = await score_conversation(
        transcript,
        source="clawops",
        clawops_summary=summary,
        client=client,
        scenario=scenario,
    )
    _replace_clawops_turns(session_id, call_id, segments, agent_speaker)
    _save_report(
        session_id,
        report,
        status="final",
        call_id=call_id,
        clawops_summary=summary,
    )
    update_report_status(
        session_id,
        "draft" if _has_scheduled_unannounced_training(session_id) else "final",
    )
    _mark_source_report_ready(session_id)
    logger.info("Final report ready session=%s call_id=%s", session_id, call_id)
    return report


def _mark_source_report_ready(result_session_id: str) -> None:
    """Expose a completed unannounced-session report on its source session."""
    with SessionLocal() as db:
        source_session_id = db.scalar(
            select(ScheduledTraining.source_session_id).where(
                ScheduledTraining.result_session_id == result_session_id
            )
        )
    if source_session_id is not None:
        update_report_status(source_session_id, "final")
        logger.info(
            "Unannounced report linked source_session=%s result_session=%s",
            source_session_id,
            result_session_id,
        )


def _has_scheduled_unannounced_training(session_id: str) -> bool:
    with SessionLocal() as db:
        return db.scalar(
            select(ScheduledTraining.id).where(
                ScheduledTraining.source_session_id == session_id,
                ScheduledTraining.status.not_in(_ABANDONED_JOB_STATUSES),
            )
        ) is not None


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


def _turn_schemas(rows: list[TranscriptTurnRecord]) -> list[TranscriptTurn]:
    return [TranscriptTurn(role=row.role, text=row.text) for row in rows]


def _josa(word: str, with_batchim: str, without_batchim: str) -> str:
    """Pick the Korean particle matching `word`'s trailing syllable's 받침."""
    for char in reversed(word):
        code = ord(char) - 0xAC00
        if 0 <= code <= 11171:
            return with_batchim if code % 28 != 0 else without_batchim
    return with_batchim


def _combine_reports(
    announced: TrainingReport | None,
    unannounced: TrainingReport | None,
) -> TrainingReport | None:
    if announced is None or unannounced is None:
        return None

    announced_risk = {item.label for item in announced.riskBehaviors}
    unannounced_risk = {item.label for item in unannounced.riskBehaviors}
    resolved_risk = sorted(announced_risk - unannounced_risk)
    repeated_risk = sorted(announced_risk & unannounced_risk)
    new_risk = sorted(unannounced_risk - announced_risk)

    announced_defense = {item.label for item in announced.defenseBehaviors}
    unannounced_defense = {item.label for item in unannounced.defenseBehaviors}
    new_defense = sorted(unannounced_defense - announced_defense)
    lost_defense = sorted(announced_defense - unannounced_defense)

    score_delta = unannounced.score - announced.score
    if score_delta > 0:
        change = f"불시 전화에서 {score_delta}점 향상됐어요."
    elif score_delta < 0:
        change = f"불시 전화에서 {abs(score_delta)}점 낮아졌어요."
    else:
        change = "두 통화의 대응 점수가 같았어요."

    summary_parts = [
        f"1차 전화는 {announced.score}점, 불시 전화는 {unannounced.score}점이었습니다. {change}"
    ]
    if resolved_risk:
        joined = ", ".join(resolved_risk)
        summary_parts.append(
            f"1차에서 나왔던 {joined}{_josa(joined, '은', '는')} 불시 전화에서 다시 나오지 않아 개선됐습니다."
        )
    if repeated_risk:
        joined = ", ".join(repeated_risk)
        summary_parts.append(
            f"{joined}{_josa(joined, '은', '는')} 불시 전화에서도 반복돼 아직 개선이 필요합니다."
        )
    if new_risk:
        joined = ", ".join(new_risk)
        summary_parts.append(
            f"불시 전화에서는 1차에 없던 {joined}{_josa(joined, '이', '가')} 새로 발견됐습니다."
        )
    if lost_defense:
        summary_parts.append(
            f"1차에서 보였던 {', '.join(lost_defense)} 방어 행동은 불시 전화에서 나타나지 않았습니다."
        )
    if new_defense:
        summary_parts.append(
            f"불시 전화에서 {', '.join(new_defense)} 방어 행동이 새로 나타났습니다."
        )

    if repeated_risk:
        joined = ", ".join(repeated_risk)
        coaching = f"{joined}{_josa(joined, '은', '는')} 두 통화 모두에서 나왔으니 이 부분부터 고쳐보세요."
    elif new_risk:
        coaching = f"불시 전화에서 새로 나온 {', '.join(new_risk)}에 대비한 대응을 연습해보세요."
    else:
        coaching = "반복되는 위험 행동이 없었어요. 지금의 대응을 계속 유지하세요."

    return TrainingReport(
        score=round((announced.score + unannounced.score) / 2),
        suspected=announced.suspected and unannounced.suspected,
        gaveName=announced.gaveName or unannounced.gaveName,
        triedHangup=announced.triedHangup and unannounced.triedHangup,
        summary=" ".join(summary_parts),
        coaching=coaching,
        # The score averages both calls, so the behaviours list both calls too:
        # listing only the unannounced call's left a low score next to "no risk
        # detected" whenever the first call held all the mistakes.
        riskBehaviors=_merge_behaviors(announced.riskBehaviors, unannounced.riskBehaviors),
        defenseBehaviors=_merge_behaviors(
            announced.defenseBehaviors, unannounced.defenseBehaviors
        ),
        source="comparison",
    )


def _merge_behaviors(
    first: list[BehaviorItem], second: list[BehaviorItem]
) -> list[BehaviorItem]:
    """Both calls' behaviours in call order, each distinct (label, evidence) once."""
    merged: list[BehaviorItem] = []
    seen: set[tuple[str, str]] = set()
    for item in [*first, *second]:
        key = (item.label, item.evidence)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


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


def _replace_clawops_turns(
    session_id: str,
    call_id: str,
    segments: Any,
    agent_speaker: str | None = None,
) -> None:
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
                    role="user" if _is_trainee(segment, agent_speaker) else "assistant",
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


def _keep_supported(
    items: list[BehaviorItem],
    allowed: tuple[str, ...],
    trainee_text: str,
) -> list[BehaviorItem]:
    normalized_trainee = _normalize_evidence(trainee_text)
    supported: list[BehaviorItem] = []
    for item in _keep_known(items, allowed):
        evidence = _normalize_evidence(item.evidence)
        if evidence and evidence in normalized_trainee:
            supported.append(item)
    return supported


def _grounded_flag(flag: bool, evidence: str, trainee_text: str) -> bool:
    """Downgrade an ungrounded suspected/gaveName/triedHangup claim to False.

    riskBehaviors/defenseBehaviors were already dropped unless their evidence
    is actually found in what the trainee said (_keep_supported below) -- these
    three summary booleans had no such check and were trusted straight off the
    LLM's JSON. Hold them to the same bar.
    """
    if not flag:
        return False
    normalized_evidence = _normalize_evidence(evidence)
    if not normalized_evidence:
        return False
    return normalized_evidence in _normalize_evidence(trainee_text)


def _trainee_text(transcript: str) -> str:
    lines = (transcript or "").splitlines()
    has_speaker_labels = any(
        line.startswith("[훈련자]") or line.startswith("[상대]") for line in lines
    )
    if not has_speaker_labels:
        return (transcript or "").strip()
    return "\n".join(
        line.split("]", 1)[-1].strip()
        for line in lines
        if line.startswith("[훈련자]") and line.split("]", 1)[-1].strip()
    )


def _normalize_evidence(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text or "").lower()


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


def score_web_events(event_types: list[str]) -> WebTrainingReport:
    """웹 훈련 이벤트 목록을 전화와 같은 척도로 채점한다.

    같은 이벤트가 중복으로 들어와도 라벨은 집합으로 모으므로 한 번만 반영된다.
    """
    seen: dict[str, str] = {}
    for event in event_types:
        label = WEB_EVENT_LABELS.get(event)
        if label is not None and label not in seen:
            seen[label] = WEB_EVENT_EVIDENCE.get(event, "웹 훈련")

    risk = [
        BehaviorItem(label=label, evidence=evidence)
        for label, evidence in seen.items()
        if label in RISK_SCORE_WEIGHTS
    ]
    defense = [
        BehaviorItem(label=label, evidence=evidence)
        for label, evidence in seen.items()
        if label in DEFENSE_SCORE_WEIGHTS
    ]
    return WebTrainingReport(
        score=calculate_response_score(risk, defense),
        events=list(dict.fromkeys(event_types)),
        riskBehaviors=risk,
        defenseBehaviors=defense,
    )


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


def _report_llm_attempts() -> list[tuple[str, Any, str]]:
    """(label, client, model) attempts for the report LLM, in priority order.

    Both providers are reachable through the OpenAI-compatible chat.completions
    shape (Gemini via its own compatible endpoint -- see
    ai/llm_stream.py, which uses the same endpoint), so the same call works against either client. Only the ones
    whose API key is actually configured are attempted; if neither is, the
    caller gets a clear error instead of an opaque auth failure.
    """
    from openai import AsyncOpenAI

    attempts: list[tuple[str, Any, str]] = []
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        attempts.append((
            "OpenAI",
            # max_retries=0: the SDK otherwise burns ~1.2s retrying a
            # credit-exhausted 429 that will never succeed. Falling over to
            # the other provider is both faster and more likely to work.
            AsyncOpenAI(api_key=openai_key, max_retries=0),
            os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
        ))
    gemini_key = os.getenv("GEMINI_API_KEY", "").strip()
    if gemini_key:
        ensure_ai_importable()
        from ai.config import GEMINI_OPENAI_BASE_URL

        attempts.append((
            "Gemini",
            AsyncOpenAI(
                api_key=gemini_key,
                base_url=GEMINI_OPENAI_BASE_URL,
                max_retries=1,
            ),
            os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
            or "gemini-3.5-flash-lite",
        ))
    return attempts


async def _report_completion(
    client: Any, model: str, system: str, user_content: str
) -> str:
    response = await client.chat.completions.create(
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content or "{}"


async def _ask_report_llm(
    transcript: str,
    *,
    source: Literal["live", "clawops"],
    clawops_summary: dict[str, Any] | None,
    client: Any | None,
    scenario: Any | None,
) -> _LlmReport:
    risk_labels, defense_labels = _behavior_labels()
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
    scenario_note = _scenario_report_note(scenario)
    system = f"""
너는 보이스피싱 모의훈련 코치다. 이 대화는 사전 동의 하의 교육 시뮬레이션이다.
실제 금감원·검찰·은행 전화가 아니다. 점수나 등급은 매기지 마라.
{source_note}
{scenario_note}

반드시 JSON 객체만 반환한다.
형식:
{{
  "suspected": true,
  "suspectedEvidence": "훈련자가 실제로 한 말 그대로 인용",
  "gaveName": false,
  "gaveNameEvidence": "",
  "triedHangup": true,
  "triedHangupEvidence": "훈련자가 실제로 한 말 그대로 인용",
  "summary": "2~4문장 한국어 요약",
  "coaching": "다음에 이렇게 하세요. 2~3문장",
  "riskBehaviors": [{{"label": "...", "evidence": "..."}}],
  "defenseBehaviors": [{{"label": "...", "evidence": "..."}}]
}}

규칙:
- suspected: 훈련자가 상대 신원·기관을 의심하거나 확인하려 했는지. true면 suspectedEvidence에 훈련자가 실제로 한 말을 그대로 인용한다. 근거로 댈 말이 없으면 suspected는 false로 한다.
- gaveName: 훈련자가 성함이나 이름을 댔는지. true면 gaveNameEvidence에 그 말을 그대로 인용한다. 근거 없으면 false로 한다.
- triedHangup: 끊겠다고 하거나 통화를 끝내려고 했는지. true면 triedHangupEvidence에 그 말을 그대로 인용한다. 근거 없으면 false로 한다.
- 모든 evidence 필드는 훈련자 발화를 그대로 가져온 짧은 인용이어야 하며, 지어내지 않는다.
- riskBehaviors label은 다음만: {", ".join(risk_labels)}
- defenseBehaviors label은 다음만: {", ".join(defense_labels)}
- evidence는 짧은 인용. 근거 없으면 그 라벨을 넣지 마라
- 추측으로 채우지 마라
""".strip()

    user_content = f"대화 기록:\n{transcript}{summary_note}"

    if client is not None:
        # Caller supplied a client explicitly (tests, or a future caller that
        # wants a specific provider) -- use exactly that, no fallback.
        raw = await _report_completion(
            client,
            os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            system,
            user_content,
        )
    else:
        attempts = _report_llm_attempts()
        if not attempts:
            raise RuntimeError(
                "Report LLM unavailable: neither OPENAI_API_KEY nor "
                "GEMINI_API_KEY is set"
            )
        raw = None
        last_error: Exception | None = None
        for label, attempt_client, model in attempts:
            try:
                raw = await _report_completion(attempt_client, model, system, user_content)
                break
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Report LLM via %s failed; trying next provider: %s",
                    label,
                    exc,
                )
        if raw is None:
            raise RuntimeError(
                "Report LLM failed on every configured provider"
            ) from last_error

    try:
        return _LlmReport.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"Report LLM returned invalid JSON: {raw[:500]}") from exc


def _scenario_report_note(scenario: Any | None = None) -> str:
    try:
        scenario = scenario or get_call_scenario()
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
