from types import SimpleNamespace

from ai.harness import audit_transcript
from ai.scenarios import get_scenario
from ai.transcript import identify_agent_speaker, label_roles

BANK = get_scenario("bank_security_hold")


def seg(speaker, text):
    return {"speaker": speaker, "start": 0.0, "end": 1.0, "text": text}


def test_old_format_agent_label_is_taken_as_is():
    segments = [seg("CUSTOMER", "여보세요"), seg("AGENT", "안녕하세요")]
    assert identify_agent_speaker(segments, BANK) == "AGENT"


def test_the_speaker_who_said_the_opening_line_is_the_agent():
    """The trainee usually speaks first ("여보세요"), so order proves nothing."""
    segments = [
        seg("speaker_0", "여보세요?"),
        seg("speaker_1", "가온금융안전원 결제보호팀 서동현입니다."),
        seg("speaker_1", "고객님 명의로 해외 결제 승인 요청이 한 건 들어와서 확인차 연락드렸습니다. 혹시 오늘 해외 사이트에서 결제하신 적 있으십니까?"),
        seg("speaker_0", "아니요 그런 적 없는데요"),
        seg("speaker_1", "그러면 명의 도용 건입니다."),
    ]
    assert identify_agent_speaker(segments, BANK) == "speaker_1"


def test_opening_heard_imperfectly_still_identifies_the_agent():
    segments = [
        seg("speaker_1", "네 여보세요"),
        seg("speaker_0", "가온 금융 안전원 결제 보호팀 서동현 입니다 고객님 명의로 해외 결제 승인 요청이 한건 들어와서요"),
        seg("speaker_1", "네? 무슨 결제요?"),
    ]
    assert identify_agent_speaker(segments, BANK) == "speaker_0"


def test_falls_back_to_script_wording_when_the_opening_is_missing():
    segments = [
        seg("speaker_0", "잠깐만요 누구시라고요"),
        seg("speaker_1", "대표번호로 다시 거시면 대기만 이십 분입니다. 그 사이에 보류 시한이 먼저 끝납니다."),
        seg("speaker_0", "그래도 확인해 볼게요"),
        seg("speaker_1", "같은 가맹점에서 이백구십만 원이 한 건 더 대기 중입니다."),
    ]
    assert identify_agent_speaker(segments, BANK) == "speaker_1"


def test_refuses_to_guess():
    assert identify_agent_speaker([seg("speaker_0", "여보세요")], BANK) is None
    assert identify_agent_speaker([], BANK) is None
    alike = [seg("speaker_0", "네 알겠습니다"), seg("speaker_1", "네 알겠습니다")]
    assert identify_agent_speaker(alike, BANK) is None


def test_accepts_sdk_objects_as_well_as_dicts():
    segments = [SimpleNamespace(speaker="speaker_0", text=BANK.opening_line),
                SimpleNamespace(speaker="speaker_1", text="네?")]
    assert identify_agent_speaker(segments, BANK) == "speaker_0"


def test_label_roles_keeps_order_and_skips_empty_text():
    segments = [seg("speaker_1", "여보세요"), seg("speaker_0", BANK.opening_line), seg("speaker_1", " ")]
    assert label_roles(segments, "speaker_0") == [
        {"role": "user", "text": "여보세요"},
        {"role": "assistant", "text": BANK.opening_line},
    ]


def test_audit_flags_slips_and_records_the_safety_exit():
    findings = audit_transcript([
        "가온금융안전원 결제보호팀 서동현입니다.",
        "국민은행 보안팀과 연결해 드리겠습니다. 비밀번호 네 자리를 말씀해 주세요.",
        "잠시만요, 통화를 멈추겠습니다. 지금 전화는 사전에 동의하신 보이스피싱 대응 훈련 전화였습니다.",
        "사실 저는 AI입니다.",
    ])
    kinds = [(f["index"], f["kind"]) for f in findings]
    assert (1, "real_org") in kinds
    assert (1, "secret_request") in kinds
    assert (2, "safety_exit") in kinds
    assert (3, "persona_break") in kinds
    assert all(index != 0 for index, _ in kinds)


def test_audit_keeps_the_trust_building_line():
    assert audit_transcript(["의심하시는 게 맞습니다. 그래서 저희는 카드 번호나 비밀번호는 절대 안 여쭙습니다."]) == []
