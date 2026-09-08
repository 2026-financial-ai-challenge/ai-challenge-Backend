import asyncio
import json
from types import SimpleNamespace

import pytest

from app.training.scenarios import ensure_ai_importable, get_call_scenario


def setup_function() -> None:
    ensure_ai_importable()


def test_generated_scenario_coerces_llm_shape():
    from ai.scenarios.generator import GeneratedScenario

    payload = {
        "name": "결제 보호 사칭",
        "persona_name": "김정훈",
        "organization": "중앙금융보호센터",
        "tone": "단정한 상담원",
        "opening_line": "안녕하세요. 확인할 거래 건으로 연락드렸습니다",
        "scenario_summary": "편의점 결제 승인 대기를 이유로 본인 확인을 요구한다.",
        "conversation_goal": "성함을 받아 임시 보호를 압박한다.",
        "turn_plan": [
            {"turn": 1, "message": "이상 결제를 설명한다."},
            {"turn": 2, "message": "본인 확인이 필요합니다."},
            {"turn": 3, "message": "의심하면 긴급성으로 대응한다."},
            {"turn": 4, "message": "거부에 한 번 더 붙잡는다."},
        ],
        "tactics": ["권위 사칭", "긴급성 조성"],
        "red_flags": ["전화로 이체를 요구함"],
        "ideal_trainee_response": [
            "이 결제에 대해 공식 번호로 확인한 뒤 끊는다."
        ],
        "difficulty": "보통",
        "max_turns": "8",
        "unused_extra": True,
    }
    generated = GeneratedScenario.model_validate(payload)
    assert generated.turn_plan[0] == "이상 결제를 설명한다."
    assert generated.ideal_trainee_response.startswith("이 결제에 대해")
    assert generated.difficulty == "중"
    assert generated.max_turns == 8
    assert generated.opening_line.endswith(".")


def test_forbidden_check_allows_training_words_and_rubric():
    from ai.scenarios.generator import GeneratedScenario, _validate_safe_text

    generated = GeneratedScenario.model_validate(
        {
            "name": "training fallback",
            "persona_name": "김정훈",
            "organization": "중앙금융보호센터",
            "tone": "단정한 상담원",
            "opening_line": "안녕하세요. 이상 거래 확인 건입니다.",
            "scenario_summary": "은행 사칭처럼 보이는 결제 보호 전화를 재현한다.",
            "conversation_goal": "개인정보 제공을 유도한다.",
            "turn_plan": [
                "사건을 설명한다.",
                "본인 확인을 요구한다.",
                "의심하면 권위로 대응한다.",
                "거부하면 한 번 붙잡는다.",
            ],
            "tactics": ["권위 사칭"],
            "red_flags": ["검찰이나 은행을 사칭하며 이체를 요구함"],
            "ideal_trainee_response": "즉시 끊고 112/1332로 확인한다.",
            "difficulty": "중",
            "max_turns": 8,
        }
    )
    _validate_safe_text(generated)


def test_forbidden_check_rejects_spoken_url():
    from ai.scenarios.generator import GeneratedScenario, _validate_safe_text

    generated = GeneratedScenario.model_validate(
        {
            "name": "링크 유도",
            "persona_name": "김정훈",
            "organization": "중앙금융보호센터",
            "tone": "급한 상담원",
            "opening_line": "안녕하세요. https://evil.example 로 접속하세요.",
            "scenario_summary": "결제 오류를 이유로 링크 접속을 유도한다.",
            "conversation_goal": "앱 설치를 유도한다.",
            "turn_plan": [
                "사건을 설명한다.",
                "본인 확인을 요구한다.",
                "의심하면 권위로 대응한다.",
                "거부하면 한 번 붙잡는다.",
            ],
            "tactics": ["권위 사칭"],
            "red_flags": ["링크 접속을 요구함"],
            "ideal_trainee_response": "전화를 끊는다.",
            "difficulty": "하",
            "max_turns": 6,
        }
    )
    with pytest.raises(ValueError, match="forbidden token"):
        _validate_safe_text(generated)


def test_generate_scenario_retries_then_succeeds():
    from ai.scenarios.generator import generate_scenario

    bad = json.dumps({"not": "a scenario"})
    good = json.dumps(
        {
            "name": "이상결제 보호 사칭",
            "persona_name": "김정훈",
            "organization": "중앙금융보호센터",
            "tone": "단정하고 급한 상담원 말투",
            "opening_line": "안녕하세요. 중앙금융보호센터 고객보호팀 김정훈입니다.",
            "scenario_summary": "오늘 오후 편의점 결제 승인 대기를 이유로 본인 확인을 요구한다.",
            "conversation_goal": "성함을 받아 내고 임시 보호 이체를 구두로 압박한다.",
            "turn_plan": [
                "이상 결제 사건을 짧게 설명한다.",
                "본인 거래인지 확인한다.",
                "의심하면 긴급성과 권위로 대응한다.",
                "개인정보 거부에 한 번 더 붙잡는다.",
            ],
            "tactics": ["권위 사칭", "긴급성 조성"],
            "red_flags": ["공식 확인 없이 전화로 이체를 요구함"],
            "ideal_trainee_response": "즉시 전화를 끊고 112 또는 1332로 확인한다.",
            "difficulty": "중",
            "max_turns": 8,
        }
    )
    review = json.dumps(
        {"valid": True, "score": 90, "issues": [], "suggestions": []}
    )
    payloads = [bad, good, review]

    class Completions:
        async def create(self, **_kwargs):
            content = payloads.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    scenario = asyncio.run(generate_scenario(get_call_scenario(), client=client))
    assert scenario.id.endswith(":dynamic")
    assert scenario.opening_line.startswith("안녕하세요.")
    assert "권위 사칭" in scenario.tactics
    assert not payloads


def test_generate_scenario_keeps_structurally_valid_after_strict_review():
    from ai.scenarios.generator import generate_scenario

    good = json.dumps(
        {
            "name": "배송결제 보호 사칭",
            "persona_name": "이민재",
            "organization": "한빛거래보호센터",
            "tone": "단정한 상담원",
            "opening_line": "안녕하세요. 한빛거래보호센터 이민재입니다. 배송 결제 건으로 연락드렸습니다.",
            "scenario_summary": "배송 결제 오류를 이유로 본인 확인을 요구한다.",
            "conversation_goal": "성함을 받아 임시 보호를 압박한다.",
            "turn_plan": [
                "결제 오류를 설명한다.",
                "본인 확인을 요구한다.",
                "의심하면 긴급성으로 대응한다.",
                "거부하면 한 번 붙잡는다.",
            ],
            "tactics": ["권위 사칭", "긴급성 조성"],
            "red_flags": ["전화로 이체를 요구함"],
            "ideal_trainee_response": "즉시 전화를 끊고 112 또는 1332로 확인한다.",
            "difficulty": "중",
            "max_turns": 8,
        }
    )
    nitpick = json.dumps(
        {
            "valid": False,
            "score": 62,
            "issues": ["opening_line이 긴급성을 충분히 전달하지 못함"],
            "suggestions": ["첫 대사에 금액을 넣으세요"],
        }
    )
    payloads = [good, nitpick]

    class Completions:
        async def create(self, **_kwargs):
            content = payloads.pop(0)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    scenario = asyncio.run(generate_scenario(get_call_scenario(), client=client))
    assert scenario.id.endswith(":dynamic")
    assert scenario.name == "배송결제 보호 사칭"
    assert scenario.opening_line.startswith("안녕하세요. 한빛거래보호센터")
    assert not payloads
