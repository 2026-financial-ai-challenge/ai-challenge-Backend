"""Generate one safe, self-contained scenario for an outbound training call."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ai.safety import SAFETY_RULES
from ai.scenarios.types import Scenario
from ai.voices import Onyu

GUIDELINES_PATH = Path(__file__).with_name("scenario_generation_guidelines.md")
_FORBIDDEN_OUTPUT = re.compile(
    r"https?://|www\.|\d{6,}|주민등록번호|카드번호|계좌번호|비밀번호|인증번호|"
    r"금감원|검찰|경찰|은행|카드사|AI|모델|프롬프트|시뮬레이션",
    re.IGNORECASE,
)


class GeneratedScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
        "ideal_trainee_response",
        mode="before",
    )
    @classmethod
    def strip_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ScenarioReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

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


def _guidelines() -> str:
    try:
        return GUIDELINES_PATH.read_text(encoding="utf-8")
    except OSError:
        return "매 통화마다 안전하고 서로 다른 교육용 시나리오를 생성한다."


def _planner_prompt(base: Scenario) -> str:
    return f"""{_guidelines()}

[고정된 원본 유형]
- id: {base.id}
- 이름: {base.name}
- 기존 최대 턴: {base.max_turns}

위 지침에 따라 이번 통화에 사용할 새로운 시나리오를 JSON 하나로만 생성한다.
JSON 키는 name, persona_name, organization, tone, opening_line,
scenario_summary, conversation_goal, turn_plan, tactics, red_flags,
ideal_trainee_response, difficulty, max_turns다.
실제 기관명과 위험한 숫자·주소·앱 정보는 절대 넣지 않는다.
"""


def _validate_safe_text(scenario: GeneratedScenario) -> None:
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
    if any(_FORBIDDEN_OUTPUT.search(value) for value in values):
        raise ValueError("generated scenario contains a forbidden token")
    if not all(value.endswith((".", "?", "!")) for value in [scenario.opening_line]):
        raise ValueError("opening_line must end with punctuation")


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
                    "논리적 일관성과 대화 흐름만 검사한다."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 시나리오를 검토한다.

{scenario.model_dump_json(ensure_ascii=False)}

검사 기준:
- opening_line이 scenario_summary의 사건과 자연스럽게 연결되는가.
- conversation_goal과 turn_plan이 서로 일치하는가.
- turn_plan이 원인과 결과가 있는 자연스러운 순서인가.
- 훈련자의 의심, 거부, 질문에 대응하는 단계가 있는가.
- red_flags가 실제 진행 단계에 반영되는가.
- ideal_trainee_response가 red_flags에 대응하는가.
- max_turns가 turn_plan을 수행하기에 충분한가.
- 인물, 기관, 사건의 세부 정보가 서로 충돌하지 않는가.

score가 80점 이상이고 치명적인 문제가 없을 때만 valid=true로 한다.
문제가 있으면 issues에 구체적으로 적고, 개선 방향은 suggestions에 적는다.
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


async def generate_scenario(base: Scenario, *, client: AsyncOpenAI | None = None) -> Scenario:
    """Generate, validate, and review a scenario for one outbound call."""
    openai_client = client or AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
    response = await openai_client.chat.completions.create(
        model=os.getenv("SCENARIO_GENERATOR_MODEL", "gpt-4o-mini"),
        messages=[
            {"role": "system", "content": "너는 안전한 교육용 통화 시나리오 설계자다."},
            {"role": "user", "content": _planner_prompt(base)},
        ],
        temperature=0.9,
        max_tokens=1000,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content or ""
    try:
        generated = GeneratedScenario.model_validate(json.loads(content))
        _validate_safe_text(generated)
        _validate_structure(generated)
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"invalid generated scenario: {exc}") from exc

    review = await review_scenario(generated, client=openai_client)
    if not review.valid or review.score < 80:
        details = "; ".join(review.issues) or "score below threshold"
        raise ValueError(f"scenario failed logical review: {details}")
    return _to_scenario(base, generated)
