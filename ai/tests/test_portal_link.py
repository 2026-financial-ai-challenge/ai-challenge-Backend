"""통화 중 포털 링크 문자 회귀 테스트."""

from pathlib import Path

from ai.safety import REAL_ORGS, SPOKEN_META, UNSAFE_TOKEN
from ai.scenarios import PLAYBOOKS
from ai.scenarios.portal_link import PORTAL_LINKS, link_for


def test_sms_body_passes_safety_gates():
    for link in PORTAL_LINKS.values():
        # 문자에는 '훈련'이라고 쓰지 않는다. 경고는 프론트가 따로 띄운다.
        assert not SPOKEN_META.search(link.sms_body), link.sms_body
        assert not REAL_ORGS.search(link.sms_body), link.sms_body
        assert not UNSAFE_TOKEN.search(link.sms_body), link.sms_body


def test_sms_names_the_portal_the_caller_mentions():
    playbooks = {p.id: p for p in PLAYBOOKS}
    for scenario_id, link in PORTAL_LINKS.items():
        assert "가온형사사법지원포털" in link.sms_body
        assert "가온형사사법지원포털" in playbooks[scenario_id].incident
        assert link.send_after_seconds > 0


def test_sms_body_matches_backend():
    # 실제 문자는 백엔드가 보낸다. 문구가 어긋나지 않게 백엔드 파일에 같은 문장이 있는지 본다
    src = (Path(__file__).parents[2] / "backend/app/services/web_training_service.py").read_text(encoding="utf-8")
    for link in PORTAL_LINKS.values():
        assert link.sms_body in src


def test_link_for():
    assert link_for("investigation_unit") is PORTAL_LINKS["investigation_unit"]
    assert link_for("card_delivery") is None
    assert link_for("") is None
