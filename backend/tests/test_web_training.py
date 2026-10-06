import asyncio

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.participant import Participant
from app.services import web_training_service
from app.services.auth_service import create_access_token, hash_password
from app.services.report_service import score_web_events
from app.services.session_service import create_session, reset_sessions
from app.services.web_training_service import (
    WEB_TRAINING_SCENARIO_ID,
    create_link,
    dispatch_training_link,
    link_exists,
)


def setup_function() -> None:
    reset_sessions()


def _client() -> TestClient:
    return TestClient(app)


def _authenticated_session() -> tuple[str, dict[str, str]]:
    with SessionLocal.begin() as db:
        participant = Participant(
            phone_number="01055557777",
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


# ---- scoring -------------------------------------------------------------


def test_score_maps_events_to_phone_labels():
    report = score_web_events(
        ["link_opened", "identity_submitted", "financial_info_submitted"]
    )
    # 60 - 15(개인정보) - 25(금융정보) = 20
    assert report.score == 20
    assert {b.label for b in report.riskBehaviors} == {"개인정보 제공", "금융정보 제공"}
    assert report.defenseBehaviors == []


def test_defensive_events_raise_the_score():
    report = score_web_events(["link_opened", "report_clicked", "left_without_input"])
    # 60 + 12(신고) + 15(빠른 판단) = 87
    assert report.score == 87


def test_neutral_only_events_keep_base_score():
    assert score_web_events(["link_opened"]).score == 60


def test_duplicate_events_count_once():
    once = score_web_events(["identity_submitted"]).score
    twice = score_web_events(["identity_submitted", "identity_submitted"]).score
    assert once == twice == 45


# ---- endpoints -----------------------------------------------------------


def test_issue_link_requires_ownership():
    client = _client()
    session_id, headers = _authenticated_session()

    forbidden = client.post(f"/v1/web-training/sessions/{session_id}/link")
    assert forbidden.status_code == 401

    issued = client.post(
        f"/v1/web-training/sessions/{session_id}/link", headers=headers
    )
    assert issued.status_code == 200
    assert issued.json()["token"]


def test_open_page_validates_token():
    client = _client()
    assert client.get("/v1/web-training/nope").status_code == 404

    session_id, _ = _authenticated_session()
    token = create_link(session_id)
    assert client.get(f"/v1/web-training/{token}").status_code == 200


def test_events_flow_into_the_report():
    client = _client()
    session_id, headers = _authenticated_session()
    token = create_link(session_id)

    for event in ("link_opened", "identity_submitted", "report_clicked"):
        posted = client.post(
            f"/v1/web-training/{token}/events", json={"eventType": event}
        )
        assert posted.status_code == 204

    report = client.get(
        f"/v1/sessions/{session_id}/report", headers=headers
    ).json()
    web = report["webTraining"]
    assert web is not None
    # 60 - 15(개인정보) + 12(신고) = 57
    assert web["score"] == 57
    assert "identity_submitted" in web["events"]


def test_unknown_event_is_rejected():
    client = _client()
    session_id, _ = _authenticated_session()
    token = create_link(session_id)
    bad = client.post(
        f"/v1/web-training/{token}/events", json={"eventType": "hack"}
    )
    assert bad.status_code == 400


# ---- auto dispatch after a call -----------------------------------------


def test_dispatch_sends_one_link_and_is_idempotent(monkeypatch):
    session_id, _ = _authenticated_session()

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web_training_service,
        "send_sms",
        lambda to, body: sent.append((to, body)) or "MG123",
    )

    asyncio.run(dispatch_training_link(session_id, WEB_TRAINING_SCENARIO_ID))
    asyncio.run(
        dispatch_training_link(session_id, WEB_TRAINING_SCENARIO_ID)
    )  # webhook re-delivery

    assert len(sent) == 1
    to, body = sent[0]
    assert to == "01055557777"
    assert "/t/" in body  # the training link
    assert link_exists(session_id)


def test_dispatch_skips_other_scenarios(monkeypatch):
    session_id, _ = _authenticated_session()

    called = False

    def fake_send(to, body):
        nonlocal called
        called = True
        return "MG"

    monkeypatch.setattr(web_training_service, "send_sms", fake_send)
    asyncio.run(dispatch_training_link(session_id, "bank_security"))

    assert called is False
    assert not link_exists(session_id)


def test_dispatch_skips_session_without_phone(monkeypatch):
    # A session with no participant has no phone number to text.
    session_id = create_session(privacy=True, unannounced_training=True).id

    called = False

    def fake_send(to, body):
        nonlocal called
        called = True
        return "MG"

    monkeypatch.setattr(web_training_service, "send_sms", fake_send)
    asyncio.run(dispatch_training_link(session_id, WEB_TRAINING_SCENARIO_ID))

    assert called is False
    assert not link_exists(session_id)


def test_repeated_event_is_idempotent():
    client = _client()
    session_id, headers = _authenticated_session()
    token = create_link(session_id)

    for _ in range(3):
        client.post(
            f"/v1/web-training/{token}/events",
            json={"eventType": "financial_info_submitted"},
        )

    report = client.get(
        f"/v1/sessions/{session_id}/report", headers=headers
    ).json()
    # 60 - 25(금융정보), counted once
    assert report["webTraining"]["score"] == 35


# ---- closing the link after a risky action -------------------------------


def test_risky_event_closes_the_link_for_reopening():
    client = _client()
    session_id, _ = _authenticated_session()
    token = create_link(session_id)

    client.post(f"/v1/web-training/{token}/events", json={"eventType": "link_opened"})
    assert client.get(f"/v1/web-training/{token}").status_code == 200

    client.post(
        f"/v1/web-training/{token}/events",
        json={"eventType": "identity_submitted"},
    )
    # 다른 기기에서 다시 열어도 서버가 막는다.
    reopened = client.get(f"/v1/web-training/{token}")
    assert reopened.status_code == 410
    assert reopened.json()["code"] == "WEB_LINK_CLOSED"


def test_defensive_event_keeps_the_link_open():
    client = _client()
    session_id, _ = _authenticated_session()
    token = create_link(session_id)

    client.post(
        f"/v1/web-training/{token}/events", json={"eventType": "report_clicked"}
    )
    assert client.get(f"/v1/web-training/{token}").status_code == 200


def test_closed_link_still_accepts_events_from_the_open_page():
    client = _client()
    session_id, headers = _authenticated_session()
    token = create_link(session_id)

    client.post(
        f"/v1/web-training/{token}/events",
        json={"eventType": "financial_info_submitted"},
    )
    # 중복 위험 이벤트가 롤백돼도 닫힘은 유지된다.
    client.post(
        f"/v1/web-training/{token}/events",
        json={"eventType": "financial_info_submitted"},
    )
    later = client.post(
        f"/v1/web-training/{token}/events", json={"eventType": "report_clicked"}
    )
    assert later.status_code == 204
    assert client.get(f"/v1/web-training/{token}").status_code == 410

    report = client.get(
        f"/v1/sessions/{session_id}/report", headers=headers
    ).json()
    # 60 - 25(금융정보) + 12(신고) = 47
    assert report["webTraining"]["score"] == 47


# ---- access log masking --------------------------------------------------


def test_access_log_masks_the_link_token():
    import logging

    from app.main import _MaskWebTrainingToken

    def masked(path: str) -> str:
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, "", 0,
            '%s - "%s %s HTTP/%s" %d',
            ("1.2.3.4:5", "GET", path, "1.1", 200), None,
        )
        _MaskWebTrainingToken().filter(record)
        return record.args[2]

    assert masked("/v1/web-training/abcDEF_123") == "/v1/web-training/***"
    assert masked("/v1/web-training/abc/events") == "/v1/web-training/***/events"
    assert (
        masked("/v1/web-training/sessions/s1/link")
        == "/v1/web-training/sessions/s1/link"
    )
    assert masked("/v1/sessions/s1/report") == "/v1/sessions/s1/report"


# ---- mid-call dispatch ---------------------------------------------------


def _connected_call(monkeypatch, scenario_id: str) -> tuple[str, list]:
    from types import SimpleNamespace

    from app.services import call_service, report_service
    from app.services.report_service import bind_call
    from app.services.session_service import attach_call

    session_id, _ = _authenticated_session()
    bind_call(session_id, "CAmid")
    attach_call(session_id, "CAmid", scenario_id=scenario_id, agent_variant="external_tts")
    monkeypatch.setattr(
        report_service,
        "_clawops_calls",
        lambda: SimpleNamespace(get=lambda _id: SimpleNamespace(status="in-progress")),
    )
    monkeypatch.setenv("MID_CALL_SMS_DELAY_SEC", "0")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web_training_service,
        "send_sms",
        lambda to, body: sent.append((to, body)) or "MG123",
    )

    async def connect_twice() -> None:
        # ClawOps may deliver the same status more than once.
        await call_service.handle_call_status_event("CAmid")
        await call_service.handle_call_status_event("CAmid")
        await asyncio.gather(*list(call_service._background_tasks))

    asyncio.run(connect_twice())
    return session_id, sent


def test_link_is_texted_during_the_call_once_it_connects(monkeypatch):
    session_id, sent = _connected_call(monkeypatch, WEB_TRAINING_SCENARIO_ID)

    assert len(sent) == 1
    assert "/t/" in sent[0][1]
    assert link_exists(session_id)


def test_no_mid_call_text_for_scenarios_without_a_link(monkeypatch):
    session_id, sent = _connected_call(monkeypatch, "bank_security_hold")

    assert sent == []
    assert not link_exists(session_id)


def test_status_callback_asks_clawops_for_the_answered_event():
    """Status names are not callback events: asking for "in-progress" meant
    ClawOps only ever reported the hang-up, so the mid-call text never fired."""
    from app.services import call_service

    events = call_service._STATUS_CALLBACK_EVENTS.split()
    assert "answered" in events
    assert "completed" in events
    assert set(events) <= {"initiated", "ringing", "answered", "completed", "transfer"}


def test_answered_callback_starts_the_timer_before_the_api_catches_up(monkeypatch):
    from types import SimpleNamespace

    from app.services import call_service, report_service
    from app.services.report_service import bind_call
    from app.services.session_service import attach_call

    session_id, _ = _authenticated_session()
    bind_call(session_id, "CAlag")
    attach_call(
        session_id, "CAlag", scenario_id=WEB_TRAINING_SCENARIO_ID, agent_variant="external_tts"
    )
    monkeypatch.setattr(
        report_service,
        "_clawops_calls",
        lambda: SimpleNamespace(get=lambda _id: SimpleNamespace(status="ringing")),
    )
    monkeypatch.setenv("MID_CALL_SMS_DELAY_SEC", "0")
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        web_training_service,
        "send_sms",
        lambda to, body: sent.append((to, body)) or "MG123",
    )

    async def answered() -> None:
        await call_service.handle_call_status_event("CAlag", callback_status="in-progress")
        await asyncio.gather(*list(call_service._background_tasks))

    asyncio.run(answered())

    assert len(sent) == 1
    assert link_exists(session_id)
