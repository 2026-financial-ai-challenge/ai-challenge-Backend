import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models.participant import Participant
from app.models.scheduled_training import ScheduledTraining
from app.models.training_session import TrainingSession
from app.schemas.report import BehaviorItem, TrainingReport, TranscriptTurn
from app.services import report_service
from app.services import call_service
from app.services.report_service import (
    append_turn,
    bind_call,
    build_draft_report,
    build_final_report,
    calculate_response_score,
    format_clawops_segments,
    format_live_turns,
    get_report,
    heuristic_report,
    register_transcript_listener,
    score_conversation,
    _scenario_report_note,
)
from app.services.session_service import create_session, reset_sessions
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
            phone_verified_at=datetime.now(timezone.utc),
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
        "triedHangup": True,
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


def test_format_live_turns_and_clawops_segments():
    text = format_live_turns(
        [
            TranscriptTurn(role="assistant", text="이상 거래입니다"),
            TranscriptTurn(role="user", text="김민수입니다"),
        ]
    )
    assert "[상대] 이상 거래입니다" in text
    assert "[훈련자] 김민수입니다" in text

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
    report = heuristic_report(transcript, source="live")
    assert report.suspected is True
    assert report.gaveName is True
    assert report.triedHangup is True
    assert report.source == "live"


def test_assistant_only_transcript_has_no_trainee_behaviors():
    transcript = (
        "[상대] 서울OO지방검찰청입니다. 본인 확인을 위해 성함을 말씀하십시오."
    )
    report = asyncio.run(
        score_conversation(
            transcript,
            source="live",
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
            source="live",
            client=_fake_openai(raw),
        )
    )
    assert report.score == 60
    assert report.gaveName is True
    assert [item.label for item in report.riskBehaviors] == ["개인정보 제공"]
    assert report.source == "live"


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
            source="live",
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
            source="live",
            client=_fake_openai("not-json"),
        )
    )
    assert report.triedHangup is True
    assert report.source == "live"


def test_report_llm_falls_back_to_gemini_when_openai_fails(monkeypatch):
    """No explicit client (the real call path): OpenAI is tried first, and a
    failure there (quota exhausted, auth revoked, ...) must not lose the
    report -- it should fall through to Gemini rather than degrading straight
    to the heuristic report."""
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
            source="live",
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
            source="live",
            client=None,
        )
    )
    # score_conversation swallows the exhausted-provider error and still
    # returns a usable (non-LLM) report rather than raising into the caller.
    assert report.triedHangup is True


def test_report_llm_raises_clearly_when_no_provider_is_configured(monkeypatch):
    monkeypatch.setattr(report_service, "_report_llm_attempts", lambda: [])

    report = asyncio.run(
        score_conversation(
            "[상대] 성함을 말씀하십시오\n[훈련자] 지금은 끊겠습니다",
            source="live",
            client=None,
        )
    )
    # Still degrades gracefully -- callers never see the missing-key error.
    assert report.triedHangup is True


def test_draft_then_final_report(monkeypatch):
    session_id = _session_id()
    bind_call(session_id, "CAtest")
    append_turn(session_id, "assistant", "이상 거래가 확인되어 연락드렸습니다.")
    append_turn(session_id, "user", "저는 김민수입니다. 끊겠습니다.")

    draft = asyncio.run(
        build_draft_report(session_id, client=_fake_openai(_llm_payload()))
    )
    assert draft.source == "live"
    stored = get_report(session_id)
    assert stored.status == "draft"
    assert stored.callId == "CAtest"
    assert len(stored.turns) == 2

    monkeypatch.setattr(
        report_service,
        "fetch_clawops_transcript",
        lambda call_id: SimpleNamespace(
            status="completed",
            segments=[
                SimpleNamespace(speaker="AGENT", text="성함 확인합니다"),
                SimpleNamespace(
                    speaker="CUSTOMER", text="김민수입니다. 끊겠습니다."
                ),
            ],
        ),
    )
    monkeypatch.setattr(
        report_service,
        "fetch_clawops_summary",
        lambda call_id: {"topic": "account alert"},
    )
    final = asyncio.run(
        build_final_report(
            session_id,
            "CAtest",
            client=_fake_openai(_llm_payload(summary="녹음 기준 최종 요약입니다.")),
        )
    )
    assert final is not None
    assert final.source == "clawops"
    stored = get_report(session_id)
    assert stored.status == "final"
    assert stored.draft is not None
    assert stored.final is not None
    assert stored.clawopsSummary == {"topic": "account alert"}


def test_unannounced_report_becomes_source_session_final(monkeypatch):
    source_session_id, _headers = _authenticated_session()
    bind_call(source_session_id, "CAannounced")
    append_turn(source_session_id, "user", "누구세요")
    asyncio.run(
        build_draft_report(
            source_session_id,
            client=_fake_openai(_llm_payload(summary="첫 번째 통화 결과")),
        )
    )

    now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
    schedule_unannounced_training(
        source_session_id,
        now=now,
        delay_seconds=1800,
    )
    monkeypatch.setenv("CLAWOPS_UNANNOUNCED_PHONE_NUMBER", "07011112222")
    monkeypatch.setattr(call_service, "start_training_calls", lambda *_args: None)
    process_due_scheduled_trainings(now=now + timedelta(minutes=31))

    with SessionLocal() as db:
        scheduled = db.scalar(select(ScheduledTraining))
        assert scheduled is not None
        assert scheduled.result_session_id is not None
        result_session_id = scheduled.result_session_id

    bind_call(result_session_id, "CAunannounced")
    append_turn(result_session_id, "assistant", "지금 바로 송금해 주세요")
    append_turn(result_session_id, "user", "공식 번호로 확인할게요")
    asyncio.run(
        build_draft_report(
            result_session_id,
            client=_fake_openai(
                _llm_payload(
                    summary="불시 전화 결과",
                    gaveName=False,
                    riskBehaviors=[],
                )
            ),
        )
    )

    combined = get_report(source_session_id)
    assert combined.status == "final"
    assert combined.callId == "CAunannounced"
    assert combined.draft is not None
    assert combined.draft.summary == "첫 번째 통화 결과"
    assert combined.unannounced is not None
    assert combined.unannounced.summary == "불시 전화 결과"
    assert combined.final is not None
    assert combined.final.source == "comparison"
    assert combined.final.score == round(
        (combined.draft.score + combined.unannounced.score) / 2
    )
    assert "1차 전화는" in combined.final.summary
    assert "불시 전화는" in combined.final.summary
    assert combined.draftTurns[0].text == "누구세요"
    assert [turn.text for turn in combined.unannouncedTurns] == [
        "지금 바로 송금해 주세요",
        "공식 번호로 확인할게요",
    ]
    assert [turn.text for turn in combined.turns] == [
        "지금 바로 송금해 주세요",
        "공식 번호로 확인할게요",
    ]

    with SessionLocal() as db:
        source = db.get(TrainingSession, source_session_id)
        assert source is not None
        assert source.report_status == "final"


def test_get_report_api_none_then_draft(monkeypatch):
    client = _client()
    session_id, headers = _authenticated_session()

    empty = client.get(f"/v1/sessions/{session_id}/report", headers=headers)
    assert empty.status_code == 200
    assert empty.json()["status"] == "none"

    missing = client.get("/v1/sessions/ses_missing/report", headers=headers)
    assert missing.status_code == 404

    bind_call(session_id, "CAapi")
    append_turn(session_id, "user", "누구세요")

    async def fake_score(
        transcript, *, source, clawops_summary=None, client=None, scenario=None
    ):
        return TrainingReport(
            suspected=True,
            gaveName=False,
            triedHangup=False,
            summary="의심했습니다.",
            coaching="공식 번호로 확인하세요.",
            source=source,
        )

    monkeypatch.setattr(report_service, "score_conversation", fake_score)
    asyncio.run(build_draft_report(session_id))
    body = client.get(f"/v1/sessions/{session_id}/report", headers=headers).json()
    assert body["status"] == "draft"
    assert body["draft"]["suspected"] is True
    session = client.get(f"/v1/sessions/{session_id}", headers=headers).json()["session"]
    assert session["callId"] == "CAapi"
    assert session["reportStatus"] == "draft"


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
    assert body["final"]["source"] == "clawops"


def test_register_transcript_listener_stores_turns():
    session_id = _session_id()

    class FakeAgent:
        def __init__(self):
            self.handlers = []

        def on(self, event):
            assert event == "transcript"

            def decorator(fn):
                self.handlers.append(fn)
                return fn

            return decorator

    agent = FakeAgent()
    register_transcript_listener(agent, session_id)
    call = SimpleNamespace(call_id="CAlive")
    asyncio.run(agent.handlers[0](call, "user", "  김민수입니다  "))
    stored = get_report(session_id)
    assert stored.callId == "CAlive"
    assert stored.turns[0].text == "김민수입니다"
    assert stored.turns[0].role == "user"


def test_reset_sessions_clears_reports():
    session_id = _session_id()
    bind_call(session_id, "CAclear")
    append_turn(session_id, "user", "hello")
    reset_sessions()
    assert get_report(session_id).status == "none"
    assert get_report(session_id).turns == []
