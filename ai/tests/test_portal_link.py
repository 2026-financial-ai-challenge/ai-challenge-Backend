"""통화 중 포털 링크 대본 회귀 테스트."""

import re
from pathlib import Path

from ai.safety import REAL_ORGS, SPOKEN_META, UNSAFE_TOKEN
from ai.scenarios import PLAYBOOKS, SCENARIOS
from ai.scenarios.portal_link import PORTAL_EVENTS, PORTAL_LINKS, TERMINAL_EVENTS, link_for


def test_portal_text_passes_safety_gates():
    for link in PORTAL_LINKS.values():
        # 문자에는 '훈련'이라고 쓰지 않는다. 경고는 프론트가 따로 띄운다.
        assert not SPOKEN_META.search(link.sms_body), link.sms_body
        for text in (link.sms_body, *link.warnings.values()):
            assert not REAL_ORGS.search(text), text
            assert not UNSAFE_TOKEN.search(text), text


def test_portal_matches_scenario_and_events():
    for scenario_id, link in PORTAL_LINKS.items():
        assert scenario_id in SCENARIOS
        assert link.scenario_id == scenario_id
        assert set(link.warnings) == set(TERMINAL_EVENTS)
        playbook = {p.id: p for p in PLAYBOOKS}[scenario_id]
        # 문자는 '방금 통화드린' 조사관이 보내고, 통화 중 말한 포털 이름과 같아야 한다
        assert playbook.persona_name in link.sms_body
        assert "가온형사사법지원포털" in link.sms_body
        assert "가온형사사법지원포털" in playbook.incident
        assert link.send_after_seconds > 0
    assert set(TERMINAL_EVENTS) <= set(PORTAL_EVENTS)


def test_events_match_backend():
    # 백엔드를 import하지 않고 WEB_EVENT_TYPES 정의만 읽어 이름이 어긋나지 않게 한다
    src = (Path(__file__).parents[2] / "backend/app/models/web_training.py").read_text(encoding="utf-8")
    block = re.search(r"WEB_EVENT_TYPES = \((.*?)\n\)", src, re.S).group(1)
    assert set(PORTAL_EVENTS) <= set(re.findall(r'"(\w+)"', block))


def test_link_for():
    assert link_for("investigation_unit") is PORTAL_LINKS["investigation_unit"]
    assert link_for("card_delivery") is None
    assert link_for("") is None
