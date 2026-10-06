import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models.call import Call
from app.models.participant import Participant
from app.models.scheduled_training import ScheduledTraining
from app.models.training_session import TrainingSession
from app.schemas.report import BehaviorItem, TrainingReport
from app.services import report_service
from app.services import call_service
from app.services.report_service import (
    bind_call,
    build_final_report,
    calculate_response_score,
    format_clawops_segments,
    get_report,
    heuristic_report,
    score_conversation,
    _combine_reports,
    _scenario_report_note,
)
from app.services.session_service import attach_call, create_session, reset_sessions
from app.training.scenarios import ensure_ai_importable, get_call_scenario
from app.services.training_scheduler import (
    process_due_scheduled_trainings,
    schedule_unannounced_training,
)
from app.services.auth_service import create_access_token, hash_password


def setup_function() -> None:
    reset_sessions()


def _client() -> TestClient:
    return TestClient(app)


def _session_id() -> str:
    return create_session(privacy=True, unannounced_training=True).id


def _authenticated_session() -> tuple[str, dict[str, str]]:
    with SessionLocal.begin() as db:
        participant = Participant(
            phone_number="01099998888",
            password_hash=hash_password("testPassword1"),
        )
        db.add(participant)
        db.flush()
        participant_id = participant.id
    session_id = create_session(
        privacy=True,
        unannounced_training=True,
        participant_id=participant_id,
    ).id
    return session_id, {
        "Authorization": f"Bearer {create_access_token(participant_id)}"
    }


def _llm_payload(**overrides) -> str:
    payload = {
        "suspected": True,
        "gaveName": True,
        "gaveNameEvidence": "김민수입니다",
        "triedHangup": True,
        "triedHangupEvidence": "끊겠습니다",
        "summary": "상대가 이상 거래를 말하며 성함을 물었다.",
        "coaching": "이름을 대지 말고 공식 번호로 다시 확인하세요.",
        "riskBehaviors": [{"label": "개인정보 제공", "evidence": "김민수입니다"}],
        "defenseBehaviors": [
            {"label": "전화 종료(빠른 판단)", "evidence": "끊겠습니다"}
        ],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _fake_openai(content: str):
    class Completions:
        async def create(self, **kwargs):
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content=content))
                ]
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions()))


def test_format_clawops_segments():
    clawops = format_clawops_segments(
        [
            {"speaker": "AGENT", "text": "성함 확인합니다"},
            {"speaker": "CUSTOMER", "text": "끊겠습니다"},
        ]
    )
    assert "[상대] 성함 확인합니다" in clawops
    assert "[훈련자] 끊겠습니다" in clawops


def test_heuristic_report_flags():
    transcript = (
        "[훈련자] 금감원 맞아요? 의심스러운데요\n"
        "[훈련자] 저는 김민수입니다\n"
        "[훈련자] 지금은 끊겠습니다"
    )
    report = heuristic_report(transcript)
    assert report.suspected is True
    assert report.gaveName is True
    assert report.triedHangup is True
    assert report.source == "clawops"


def test_assistant_only_transcript_has_no_trainee_behaviors():
    transcript = (
        "[상대] 서울OO지방검찰청입니다. 본인 확인을 위해 성함을 말씀하십시오."
    )
    report = asyncio.run(
        score_conversation(
            transcript,
            client=_fake_openai(_llm_payload()),
        )
    )
    assert report.score == 60
    assert report.suspected is False
    assert report.gaveName is False
    assert report.triedHangup is False
    assert report.riskBehaviors == []
    assert report.defenseBehaviors == []


def test_response_score_applies_behavior_weights_and_clamps():
    score = calculate_response_score(
        [
            BehaviorItem(label="개인정보 제공", evidence="이름을 말함"),
            BehaviorItem(label="금융정보 제공", evidence="계좌번호를 말함"),
        ],
        [
            BehaviorItem(label="상대방 신원 확인", evidence="어디 소속인가요"),
            BehaviorItem(label="전화 종료(빠른 판단)", evidence="끊겠습니다"),
        ],
    )
    assert score == 43

    assert calculate_response_score(
        [
            BehaviorItem(label=label, evidence="x")
            for label in (
                "개인정보 제공",
                "금융정보 제공",
                "상대방 기관명 신뢰",
                "송금 의사 표현",
                "링크 접근 의사",
                "앱 설치 의사",
                "통화 장시간 지속",
            )
        ],
        [],
    ) == 0
    assert calculate_response_score(
        [],
        [
            BehaviorItem(label=label, evidence="x")
            for label in (
                "상대방 신원 확인",
                "공식 대표번호 확인 의사",
                "개인정보 제공 거절",
                "송금 거절",
                "전화 종료(빠른 판단)",
                "신고 의사 표현",
            )
        ],
    ) == 100

    assert calculate_response_score(
        [],
        [BehaviorItem(label="송금 거절", evidence="x")] * 2,
    ) == 80


def test_score_conversation_uses_llm_and_filters_labels():
    raw = _llm_payload(
        riskBehaviors=[
            {"label": "개인정보 제공", "evidence": "김민수입니다"},
            {"label": "없는 라벨", "evidence": "x"},
        ]
    )
    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 김민수입니다. 끊겠습니다",
            client=_fake_openai(raw),
        )
    )
    assert report.score == 60
    assert report.gaveName is True
    assert [item.label for item in report.riskBehaviors] == ["개인정보 제공"]
    assert report.source == "clawops"


def test_score_conversation_rejects_assistant_evidence():
    raw = _llm_payload(
        riskBehaviors=[
            {"label": "개인정보 제공", "evidence": "성함을 말씀하십시오"},
        ],
        defenseBehaviors=[
            {"label": "전화 종료(빠른 판단)", "evidence": "끊겠습니다"},
        ],
    )
    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 끊겠습니다",
            client=_fake_openai(raw),
        )
    )
    assert report.score == 75
    assert report.riskBehaviors == []
    assert [item.label for item in report.defenseBehaviors] == [
        "전화 종료(빠른 판단)"
    ]


def test_report_prompt_uses_jsonl_rubric(monkeypatch):
    scenario = SimpleNamespace(
        tactics=("권위 사칭",),
        red_flags=("기관이 전화로 송금을 요구함",),
        ideal_trainee_response="전화를 끊고 112/1332로 확인한다.",
    )
    note = _scenario_report_note(scenario)
    assert "권위 사칭" in note
    assert "알아챘어야 할 위험 신호" in note
    assert "112/1332" in note


def test_score_conversation_falls_back_on_bad_json():
    report = asyncio.run(
        score_conversation(
            "[훈련자] 지금은 끊겠습니다",
            client=_fake_openai("not-json"),
        )
    )
    assert report.triedHangup is True
    assert report.source == "clawops"


def test_report_llm_falls_back_to_gemini_when_openai_fails(monkeypatch):
    monkeypatch.setattr(
        report_service,
        "_report_llm_attempts",
        lambda: [("OpenAI", "openai-client", "gpt-4o-mini"), ("Gemini", "gemini-client", "gemini-x")],
    )
    calls: list[str] = []

    async def fake_completion(client, model, system, user_content):
        calls.append(client)
        if client == "openai-client":
            raise RuntimeError("429 insufficient_quota")
        return _llm_payload(summary="Gemini로 대체 생성된 요약")

    monkeypatch.setattr(report_service, "_report_completion", fake_completion)

    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 확인해보겠습니다",
            client=None,
        )
    )
    assert calls == ["openai-client", "gemini-client"]
    assert report.summary == "Gemini로 대체 생성된 요약"


def test_report_llm_degrades_to_heuristic_when_every_provider_fails(monkeypatch):
    monkeypatch.setattr(
        report_service,
        "_report_llm_attempts",
        lambda: [("OpenAI", "openai-client", "gpt-4o-mini"), ("Gemini", "gemini-client", "gemini-x")],
    )

    async def always_fails(client, model, system, user_content):
        raise RuntimeError(f"{client} unavailable")

    monkeypatch.setattr(report_service, "_report_completion", always_fails)

    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 지금은 끊겠습니다",
            client=None,
        )
    )
    assert report.triedHangup is True


def test_report_llm_raises_clearly_when_no_provider_is_configured(monkeypatch):
    monkeypatch.setattr(report_service, "_report_llm_attempts", lambda: [])

    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 지금은 끊겠습니다",
            client=None,
        )
    )
    assert report.triedHangup is True


def test_final_report_is_scored_against_the_scenario_the_call_ran(monkeypatch):
    ensure_ai_importable()
    from ai.scenarios import SCENARIOS

    ran = next(sid for sid in SCENARIOS if sid != get_call_scenario().id)

    session_id = _session_id()
    bind_call(session_id, "CAscenario")
    attach_call(session_id, "CAscenario", scenario_id=ran, agent_variant="live")

    with SessionLocal() as db:
        stored_call = db.scalar(
            select(Call).where(Call.clawops_call_id == "CAscenario")
        )
        assert stored_call.scenario_id == ran
        assert stored_call.agent_variant == "live"

    monkeypatch.setattr(
        report_service,
        "fetch_clawops_transcript",
        lambda call_id: SimpleNamespace(
            status="completed",
            segments=[SimpleNamespace(speaker="CUSTOMER", text="안 믿어요. 끊을게요.")],
        ),
    )
    monkeypatch.setattr(report_service, "fetch_clawops_summary", lambda call_id: None)

    seen = {}

    async def fake_score(transcript, **kwargs):
        seen["scenario_id"] = getattr(kwargs.get("scenario"), "id", None)
        return heuristic_report(transcript)

    monkeypatch.setattr(report_service, "score_conversation", fake_score)

    asyncio.run(build_final_report(session_id, "CAscenario"))

    assert seen["scenario_id"] == ran


def _clawops_report(monkeypatch, session_id, call_id, segments, summary, clawops_summary=None):
    bind_call(session_id, call_id)
    monkeypatch.setattr(
        report_service,
        "fetch_clawops_transcript",
        lambda _call_id: SimpleNamespace(
            status="completed",
            segments=[
                SimpleNamespace(speaker=speaker, text=text)
                for speaker, text in segments
            ],
        ),
    )
    monkeypatch.setattr(
        report_service, "fetch_clawops_summary", lambda _call_id: clawops_summary
    )
    asyncio.run(
        build_final_report(
            session_id, call_id, client=_fake_openai(_llm_payload(summary=summary))
        )
    )


def test_call_report_carries_its_recorded_turns(monkeypatch):
    session_id = _session_id()
    _clawops_report(
        monkeypatch,
        session_id,
        "CAmanaged",
        [("AGENT", "성함 확인합니다"), ("CUSTOMER", "김민수입니다. 끊겠습니다.")],
        "녹음 기준 요약",
        clawops_summary={"topic": "account alert"},
    )

    stored = get_report(session_id)

    assert stored.status == "final"
    assert stored.callId == "CAmanaged"
    assert stored.clawopsSummary == {"topic": "account alert"}
    assert stored.draft is not None and stored.draft.summary == "녹음 기준 요약"
    assert stored.unannounced is None
    assert stored.final is None
    assert [turn.text for turn in stored.turns] == [
        "성함 확인합니다",
        "김민수입니다. 끊겠습니다.",
    ]
    assert len(stored.draftTurns) == 2


def test_first_and_unannounced_reports_are_compared(monkeypatch):
    source_session_id, _headers = _authenticated_session()
    _clawops_report(
        monkeypatch,
        source_session_id,
        "CAfirst",
        [("AGENT", "검찰청입니다"), ("CUSTOMER", "김민수입니다")],
        "첫 번째 통화 결과",
    )
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(source_session_id, now=now, delay_seconds=1800)

    waiting = get_report(source_session_id)
    assert waiting.status == "draft"
    assert waiting.draft is not None and waiting.draft.summary == "첫 번째 통화 결과"
    assert waiting.final is None
    assert [turn.text for turn in waiting.draftTurns] == ["검찰청입니다", "김민수입니다"]

    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07011112222")
    monkeypatch.setattr(call_service, "start_training_calls", lambda *_args: None)
    process_due_scheduled_trainings(now=now + timedelta(minutes=31))
    with SessionLocal() as db:
        result_session_id = db.scalar(select(ScheduledTraining)).result_session_id
    _clawops_report(
        monkeypatch,
        result_session_id,
        "CAsecond",
        [("AGENT", "택배 기사입니다"), ("CUSTOMER", "공식 번호로 확인할게요")],
        "불시 전화 결과",
    )

    combined = get_report(source_session_id)
    assert combined.status == "final"
    assert combined.callId == "CAsecond"
    assert combined.draft is not None and combined.draft.summary == "첫 번째 통화 결과"
    assert combined.unannounced is not None
    assert combined.unannounced.summary == "불시 전화 결과"
    assert combined.final is not None and combined.final.source == "comparison"
    assert [turn.text for turn in combined.draftTurns] == ["검찰청입니다", "김민수입니다"]
    assert [turn.text for turn in combined.unannouncedTurns] == [
        "택배 기사입니다",
        "공식 번호로 확인할게요",
    ]
    assert combined.final.score == round((combined.draft.score + combined.unannounced.score) / 2)
    assert "1차 전화는" in combined.final.summary
    with SessionLocal() as db:
        assert db.get(TrainingSession, source_session_id).report_status == "final"


def test_failed_unannounced_call_leaves_first_report_and_no_comparison(monkeypatch):
    source_session_id, _headers = _authenticated_session()
    _clawops_report(
        monkeypatch,
        source_session_id,
        "CAonly",
        [("AGENT", "검찰청입니다"), ("CUSTOMER", "누구세요")],
        "첫 번째 통화 결과",
    )
    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(source_session_id, now=now, delay_seconds=1800)
    with SessionLocal.begin() as db:
        db.scalar(select(ScheduledTraining)).status = "failed"

    stored = get_report(source_session_id)

    assert stored.status == "final"
    assert stored.draft is not None and stored.draft.summary == "첫 번째 통화 결과"
    assert stored.unannounced is None
    assert stored.final is None
    assert [turn.text for turn in stored.draftTurns] == ["검찰청입니다", "누구세요"]


def test_final_report_lists_both_calls_behaviors_and_averages_the_score():
    announced = TrainingReport(
        score=5,
        suspected=False,
        gaveName=True,
        triedHangup=False,
        summary="1차",
        coaching="1차 코칭",
        riskBehaviors=[
            BehaviorItem(label="개인정보 제공", evidence="류상준입니다."),
            BehaviorItem(label="통화 장시간 지속", evidence="네, 네."),
        ],
        defenseBehaviors=[],
        source="clawops",
    )
    unannounced = TrainingReport(
        score=83,
        suspected=True,
        gaveName=False,
        triedHangup=True,
        summary="불시",
        coaching="불시 코칭",
        riskBehaviors=[BehaviorItem(label="통화 장시간 지속", evidence="네, 네.")],
        defenseBehaviors=[
            BehaviorItem(label="전화 종료(빠른 판단)", evidence="끊을게요."),
        ],
        source="clawops",
    )

    final = _combine_reports(announced, unannounced)

    assert final is not None
    assert final.score == 44
    assert [(b.label, b.evidence) for b in final.riskBehaviors] == [
        ("개인정보 제공", "류상준입니다."),
        ("통화 장시간 지속", "네, 네."),
    ]
    assert [b.label for b in final.defenseBehaviors] == ["전화 종료(빠른 판단)"]
    assert len(unannounced.riskBehaviors) == 1


def test_get_report_api_moves_from_none_to_final():
    client = _client()
    session_id, headers = _authenticated_session()

    empty = client.get(f"/v1/sessions/{session_id}/report", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["status"] == "none"

    missing = client.get("/v1/sessions/ses_missing/report", headers=headers)
    assert missing.status_code == 404

    bind_call(session_id, "CAapi")
    assert client.get(f"/v1/sessions/{session_id}/report", headers=headers).json()[
        "status"
    ] == "pending"

    report_service._save_report(
        session_id,
        TrainingReport(
            suspected=True,
            gaveName=False,
            triedHangup=False,
            summary="의심했습니다.",
            coaching="공식 번호로 확인하세요.",
            source="clawops",
        ),
        status="final",
        call_id="CAapi",
    )
    body = client.get(f"/v1/sessions/{session_id}/report", headers=headers).json()
    assert body["status"] == "final"
    assert body["draft"]["suspected"] is True
    session = client.get(f"/v1/sessions/{session_id}", headers=headers).json()["session"]
    assert session["callId"] == "CAapi"


def test_transcript_webhook_builds_final(monkeypatch):
    client = _client()
    session_id, headers = _authenticated_session()
    bind_call(session_id, "CAhook")
    monkeypatch.delenv("CLAWOPS_WEBHOOK_SIGNING_SECRET", raising=False)

    async def fake_final(sid, call_id, *, client=None):
        assert sid == session_id
        assert call_id == "CAhook"
        report = TrainingReport(
            suspected=True,
            gaveName=False,
            triedHangup=True,
            summary="최종",
            coaching="끊으세요",
            source="clawops",
        )
        report_service._save_report(
            sid,
            report,
            status="final",
            call_id=call_id,
        )
        from app.services.session_service import update_report_status

        update_report_status(sid, "final")
        return report

    monkeypatch.setattr(report_service, "build_final_report", fake_final)

    response = client.post(
        "/v1/webhooks/clawops/transcript",
        data={
            "Event": "transcript.completed",
            "CallId": "CAhook",
            "AccountId": "ACtest",
            "From": "07012345678",
            "To": "01012345678",
            "Direction": "outbound",
            "Timestamp": "2026-08-24T12:00:00Z",
            "TranscriptUrl": "https://example.test/t",
            "DurationSec": "12",
            "SegmentCount": "2",
        },
    )
    assert response.status_code == 204
    body = client.get(f"/v1/sessions/{session_id}/report", headers=headers).json()
    assert body["status"] == "final"
    assert body["draft"]["source"] == "clawops"


def test_reset_sessions_clears_reports():
    session_id = _session_id()
    bind_call(session_id, "CAclear")
    report_service._save_report(
        session_id,
        heuristic_report("[훈련자] hello"),
        status="final",
        call_id="CAclear",
    )
    reset_sessions()
    assert get_report(session_id).status == "none"
    assert get_report(session_id).turns == []
