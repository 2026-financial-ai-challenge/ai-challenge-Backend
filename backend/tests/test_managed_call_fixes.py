import logging

from app.services import report_service
from app.training.scenarios import ensure_ai_importable

ensure_ai_importable()
from ai.scenarios import get_scenario  # noqa: E402


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
        {"speaker": "speaker_0", "text": "저는 AI 상담원입니다."},
    ]
    with caplog.at_level(logging.WARNING, logger=report_service.logger.name):
        report_service._audit_agent_speech("CA1", segments, "speaker_1")
    audits = [r for r in caplog.records if "Safety audit" in r.getMessage()]
    assert len(audits) == 1 and "persona_break" in audits[0].getMessage()
