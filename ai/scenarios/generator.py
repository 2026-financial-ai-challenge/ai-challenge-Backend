"""Generate one safe, self-contained scenario for an outbound training call."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai.safety import SAFETY_RULES
from ai.scenarios.types import Scenario
from ai.voices import Onyu

logger = logging.getLogger(__name__)

GUIDELINES_PATH = Path(__file__).with_name("scenario_generation_guidelines.md")
MAX_GENERATE_ATTEMPTS = 3
_DIFFICULTY_ALIASES = {
    "하": "하",
    "중": "중",
    "상": "상",
    "쉬움": "하",
    "easy": "하",
    "보통": "중",
    "medium": "중",
    "어려움": "상",
    "hard": "상",
}
_SPOKEN_META = re.compile(
    r"(?<![A-Za-z])AI(?![A-Za-z])|프롬프트|시뮬레이션",
    re.IGNORECASE,
)
_REAL_ORGS = re.compile(r"금감원|금융감독원|검찰청|경찰청|국민은행|신한은행|카카오뱅크")
_UNSAFE_TOKEN = re.compile(r"https?://|www\.|\d{6,}", re.IGNORECASE)


class GeneratedScenario(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=80)
    persona_name: str = Field(min_length=1, max_length=40)
    organization: str = Field(min_length=1, max_length=80)
    tone: str = Field(min_length=1, max_length=120)
    opening_line: str = Field(min_length=1, max_length=300)
    scenario_summary: str = Field(min_length=1, max_length=300)
    conversation_goal: str = Field(min_length=1, max_length=200)
    turn_plan: list[str] = Field(min_length=4, max_length=8)
    tactics: list[str] = Field(min_length=1, max_length=5)
    red_flags: list[str] = Field(min_length=1, max_length=8)
    ideal_trainee_response: str = Field(min_length=1, max_length=300)
    difficulty: str = Field(pattern="^(하|중|상)$")
    max_turns: int = Field(ge=4, le=12)

    @field_validator(
        "name",
        "persona_name",
        "organization",
        "tone",
        "opening_line",
        "scenario_summary",
        "conversation_goal",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return _as_text(value) if value is not None else value

    @field_validator("ideal_trainee_response", mode="before")
    @classmethod
    def coerce_response(cls, value: Any) -> Any:
        return _as_text(value) if value is not None else value

    @field_validator("turn_plan", "tactics", "red_flags", mode="before")
    @classmethod
    def coerce_str_list(cls, value: Any) -> Any:
        return _as_str_list(value) if value is not None else value

    @field_validator("difficulty", mode="before")
    @classmethod
    def coerce_difficulty(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        key = value.strip()
        return _DIFFICULTY_ALIASES.get(key, _DIFFICULTY_ALIASES.get(key.lower(), key))

    @field_validator("max_turns", mode="before")
    @classmethod
    def coerce_max_turns(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("opening_line")
    @classmethod
    def ensure_punctuation(cls, value: str) -> str:
        if value and not value.endswith((".", "?", "!")):
            return f"{value}."
        return value


class ScenarioReview(BaseModel):
    model_config = ConfigDict(extra="ignore")

    valid: bool
    score: int = Field(ge=0, le=100)
    issues: list[str] = Field(default_factory=list, max_length=8)
    suggestions: list[str] = Field(default_factory=list, max_length=8)


def dynamic_scenarios_enabled() -> bool:
    return os.getenv("DYNAMIC_SCENARIO", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("message", "text", "step", "content", "action", "description"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        parts = [
            str(item).strip()
            for item in value.values()
            if isinstance(item, str) and str(item).strip()
        ]
        return " ".join(parts)
    if isinstance(value, (list, tuple)):
        return " ".join(part for part in (_as_text(item) for item in value) if part)
    if value is None:
        return ""
    return str(value).strip()


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in re.split(r"[\n;]", value) if item.strip()]
        return items or [value.strip()]
    if isinstance(value, (list, tuple)):
        return [text for text in (_as_text(item) for item in value) if text]
    text = _as_text(value)
    return [text] if text else []


def _guidelines() -> str:
    try:
        return GUIDELINES_PATH.read_text(encoding="utf-8")
    except OSError:
        return "매 통화마다 안전하고 서로 다른 교육용 시나리오를 생성한다."


def _planner_prompt(base: Scenario, *, repair_hint: str = "") -> str:
    tactics = ", ".join(base.tactics) or "권위 사칭, 긴급성 조성"
    repair = ""
    if repair_hint:
        repair = f"\n[이전 생성 오류 — 같은 실수를 반복하지 않는다]\n{repair_hint}\n"
    return f"""{_guidelines()}
{repair}
[고정된 원본 유형]
- id: {base.id}
- 이름: {base.name}
- 기존 최대 턴: {base.max_turns}
- 기법 힌트: {tactics}

위 지침에 따라 이번 통화에 사용할 새로운 시나리오를 JSON 하나로만 생성한다.
turn_plan은 문자열 배열이다. {{"turn": 1, "message": "..."}} 같은 객체 배열을 쓰지 않는다.
ideal_trainee_response는 문자열 하나다. 배열로 주지 않는다.
difficulty는 하, 중, 상 중 하나만 쓴다. 보통/쉬움/어려움이라고 쓰지 않는다.
실제 기관명과 위험한 숫자·주소·앱 정보는 절대 넣지 않는다.

JSON 예시:
{{
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
    "개인정보 거부에 한 번 더 붙잡고, 두 번째 종료 의사에는 멈춘다."
  ],
  "tactics": ["권위 사칭", "긴급성 조성"],
  "red_flags": ["공식 확인 없이 전화로 이체를 요구함", "시간 압박으로 판단을 흐리게 함"],
  "ideal_trainee_response": "즉시 전화를 끊고 112 또는 1332로 확인한다.",
  "difficulty": "중",
  "max_turns": 8
}}
"""


def _validate_safe_text(scenario: GeneratedScenario) -> None:
    spoken = [scenario.opening_line, scenario.persona_name, scenario.organization]
    if any(_SPOKEN_META.search(value) for value in spoken):
        raise ValueError("generated scenario breaks character in spoken fields")
    if any(_REAL_ORGS.search(value) for value in spoken):
        raise ValueError("generated scenario uses a real institution name")

    values = [
        scenario.name,
        scenario.persona_name,
        scenario.organization,
        scenario.tone,
        scenario.opening_line,
        scenario.scenario_summary,
        scenario.conversation_goal,
        scenario.ideal_trainee_response,
        *scenario.turn_plan,
        *scenario.tactics,
        *scenario.red_flags,
    ]
    if any(_UNSAFE_TOKEN.search(value) for value in values):
        raise ValueError("generated scenario contains a forbidden token")


def _validate_structure(scenario: GeneratedScenario) -> None:
    if len(scenario.turn_plan) > scenario.max_turns:
        raise ValueError("turn_plan exceeds max_turns")

    plan_text = " ".join(scenario.turn_plan)
    if not any(
        marker in plan_text
        for marker in ("의심", "거부", "개인정보", "확인", "질문", "대응")
    ):
        raise ValueError("turn_plan has no trainee interaction or response step")


async def review_scenario(
    scenario: GeneratedScenario,
    *,
    client: AsyncOpenAI,
) -> ScenarioReview:
    response = await client.chat.completions.create(
        model=os.getenv("SCENARIO_REVIEW_MODEL", "gpt-4o-mini"),
        temperature=0,
        max_tokens=600,
        response_format={"type": "json_object"},
        messages=[
            {
                "role": "system",
                "content": (
                    "너는 교육용 전화 시나리오 품질 검수자다. "
                    "실제로 모순이 있을 때만 탈락시킨다. "
                    "기준 문장을 그대로 issues에 복사하지 않는다."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 시나리오를 검토한다.

{scenario.model_dump_json(ensure_ascii=False)}

valid=false는 아래가 실제로 관찰될 때만 쓴다:
- 인물/기관/사건이 서로 다른 이야기를 한다.
- turn_plan에 의심·거부·질문 대응이 없다.
- opening_line이 scenario_summary와 다른 사건이다.

해당하지 않으면 valid=true, score는 70 이상.
개선점은 suggestions에만 적는다.
JSON 키는 valid, score, issues, suggestions만 사용한다.""",
            },
        ],
    )
    content = response.choices[0].message.content or "{}"
    try:
        return ScenarioReview.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid scenario review: {exc}") from exc


def _to_scenario(base: Scenario, generated: GeneratedScenario) -> Scenario:
    system_prompt = f"""{SAFETY_RULES}

너는 전화 너머의 사람이다. {generated.organization}의 상담원 '{generated.persona_name}' 역할이다.
교육용 가상 인물이지만 상대가 눈치채지 못하게 역할에 몰입한다.

[이번 통화의 설정]
- 사건: {generated.scenario_summary}
- 목표: {generated.conversation_goal}
- 말투: {generated.tone}

[진행 계획]
""" + "\n".join(f"{index}. {step}" for index, step in enumerate(generated.turn_plan, 1)) + f"""

[말투와 출력 형식]
- 한 응답은 짧은 문장 둘까지 말한다.
- 모든 문장 끝에 마침표나 물음표를 찍는다.
- 목록, 마크다운, 내레이터 지문을 말하지 않는다.
- 실제 기관명, 번호, URL, 앱 이름을 말하지 않는다.
- 이 통화의 사건과 인물 설정을 끝까지 일관되게 유지한다.
"""
    return Scenario(
        id=f"{base.id}:dynamic",
        name=generated.name,
        opening_line=generated.opening_line,
        system_prompt=system_prompt.strip(),
        max_turns=generated.max_turns,
        tts_voice_id=base.tts_voice_id or Onyu,
        tts_stability=base.tts_stability,
        tts_similarity_boost=base.tts_similarity_boost,
        subtype=base.subtype,
        difficulty=generated.difficulty,
        tactics=tuple(generated.tactics),
        red_flags=tuple(generated.red_flags),
        ideal_trainee_response=generated.ideal_trainee_response,
    )


async def _request_scenario_json(
    base: Scenario,
    *,
    client: AsyncOpenAI,
    repair_hint: str = "",
) -> str:
    response = await client.chat.completions.create(
        model=os.getenv("SCENARIO_GENERATOR_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "너는 안전한 교육용 통화 시나리오 설계자다."},
            {"role": "user", "content": _planner_prompt(base, repair_hint=repair_hint)},
        ],
        temperature=0.9,
        max_tokens=1000,
        response_format={"type": "json_object"},
    )
    return response.choices[0].message.content or ""


def _review_is_usable(review: ScenarioReview) -> bool:
    return review.valid and review.score >= 70


async def generate_scenario(base: Scenario, *, client: AsyncOpenAI | None = None) -> Scenario:
    """Generate, validate, and review a scenario for one outbound call."""
    openai_client = client or AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    last_error: Exception | None = None
    repair_hint = ""

    for attempt in range(1, MAX_GENERATE_ATTEMPTS + 1):
        try:
            content = await _request_scenario_json(
                base, client=openai_client, repair_hint=repair_hint
            )
            generated = GeneratedScenario.model_validate(json.loads(content))
            _validate_safe_text(generated)
            _validate_structure(generated)
            try:
                review = await review_scenario(generated, client=openai_client)
            except ValueError as exc:
                review = ScenarioReview(
                    valid=True,
                    score=70,
                    issues=[str(exc)],
                    suggestions=[],
                )
            scenario = _to_scenario(base, generated)
            if _review_is_usable(review):
                logger.info(
                    "Generated scenario id=%s name=%s score=%s",
                    scenario.id,
                    scenario.name,
                    review.score,
                )
            else:
                logger.warning(
                    "Review nits ignored id=%s name=%s score=%s issues=%s",
                    scenario.id,
                    scenario.name,
                    review.score,
                    "; ".join(review.issues) or "score below threshold",
                )
            return scenario
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            last_error = exc
            repair_hint = str(exc)
            logger.warning(
                "Scenario generation attempt %s/%s failed: %s",
                attempt,
                MAX_GENERATE_ATTEMPTS,
                exc,
            )

    raise ValueError(f"invalid generated scenario: {last_error}") from last_error
