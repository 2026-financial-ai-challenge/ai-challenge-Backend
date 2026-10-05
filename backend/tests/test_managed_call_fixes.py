"""Managed-call fixes that need no database.

Run: DATABASE_URL=postgresql+psycopg://u:p@localhost:1/x \
     python -m pytest --noconftest tests/test_managed_call_fixes.py
"""

import asyncio
import logging
from types import SimpleNamespace

from app.services import call_service, report_service
from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()
from ai.scenarios import get_scenario  # noqa: E402


def test_managed_call_is_not_monitored_in_process(monkeypatch):
    # A managed call has no in-process agent; its Call has no wait(), so
    # monitoring it marked the session failed and re-queued the surprise call.
    async def fake_create(_session_id):
        return None, SimpleNamespace(call_id="CA1"), None

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("_monitor_call ran for a managed call")

    monkeypatch.setattr(call_service, "_create_outbound_call", fake_create)
    monkeypatch.setattr(call_service, "_monitor_call", must_not_run)
    asyncio.run(call_service._start_and_monitor_call("ses_1"))


def test_speaker_n_transcript_finds_the_trainee():
    scenario = get_scenario("family_emergency")
    segments = [
        {"speaker": "speaker_1", "text": scenario.opening_line},
        {"speaker": "speaker_0", "text": "누구세요? 끊을게요."},
    ]
    agent = report_service.agent_speaker_for(segments, scenario)
    assert agent == "speaker_1"
    assert report_service.format_clawops_segments(segments, agent).splitlines() == [
        f"[상대] {scenario.opening_line}",
        "[훈련자] 누구세요? 끊을게요.",
    ]


def test_old_labels_still_work_without_an_agent_speaker():
    segments = [
        {"speaker": "AGENT", "text": "확인합니다"},
        {"speaker": "CUSTOMER", "text": "끊겠습니다"},
    ]
    assert report_service.format_clawops_segments(segments) == "[상대] 확인합니다\n[훈련자] 끊겠습니다"


def test_agent_slips_are_logged(caplog):
    segments = [
        {"speaker": "speaker_1", "text": "저는 AI 상담원입니다."},
        {"speaker": "speaker_0", "text": "저는 AI 상담원입니다."},  # the trainee may say anything
    ]
    with caplog.at_level(logging.WARNING, logger=report_service.logger.name):
        report_service._audit_agent_speech("CA1", segments, "speaker_1")
    audits = [r for r in caplog.records if "Safety audit" in r.getMessage()]
    assert len(audits) == 1 and "persona_break" in audits[0].getMessage()
