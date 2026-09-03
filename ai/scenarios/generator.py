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

from ai.config import (
    GEMINI_OPENAI_BASE_URL,
    gemini_api_key,
    openai_api_key,
    scenario_llm_provider,
)
from ai.safety import SAFETY_RULES
from ai.scenarios.types import Scenario
from ai.voices import Onyu

logger = logging.getLogger(__name__)

GUIDELINES_PATH = Path(__file__).with_name("scenario_generation_guidelines.md")
MAX_GENERATE_ATTEMPTS = 3
_DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    # Non-"lite"/non-"3.x" Gemini flash models spend hundreds of hidden
    # reasoning tokens before any visible output (measured: 28-85s latency,
    # sometimes empty content). flash-lite skips that and answers in ~1s.
    "gemini": "gemini-3.5-flash-lite",
}
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
    r"(?<![A-Za-z])AI(?![A-Za-z])|모델|프롬프트|훈련|시뮬레이션",
    re.IGNORECASE,
)
# Broad, not just the handful of names the model is most likely to reach for —
# this list is checked against every field that can end up in the live system
# prompt, so it is worth erring toward too many real institutions rather than
# too few.
_REAL_ORGS = re.compile(
    r"금융감독원|금감원|검찰청|경찰청|지방경찰청|사이버수사대|국세청|관세청|"
    r"금융위원회|개인정보보호위원회|건강보험공단|국민연금공단|"
    r"국민은행|KB국민은행|신한은행|우리은행|하나은행|기업은행|IBK기업은행|"
    r"농협은행|NH농협|수협은행|새마을금고|신협|우체국|저축은행|"
    r"신한카드|삼성카드|현대카드|국민카드|KB국민카드|롯데카드|하나카드|우리카드|비씨카드|"
    r"카카오뱅크|케이뱅크|토스뱅크|토스|카카오페이|네이버페이|페이코|"
    r"쿠팡|배달의민족|CJ대한통운|대한통운|우체국택배|롯데택배|한진택배"
)
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


def _build_client() -> AsyncOpenAI:
    provider = scenario_llm_provider()
    if provider == "gemini":
        # Gemini's OpenAI-compatible endpoint accepts the same chat.completions
        # calls (including response_format={"type": "json_object"}) this module
        # already makes, so no other code here needs to change.
        return AsyncOpenAI(api_key=gemini_api_key(), base_url=GEMINI_OPENAI_BASE_URL)
    if provider == "openai":
        return AsyncOpenAI(api_key=openai_api_key())
    raise ValueError(
        f"Unknown SCENARIO_LLM_PROVIDER={provider!r} (expected 'openai' or 'gemini')"
    )


def _resolve_model(env_name: str) -> str:
    provider = scenario_llm_provider()
    default = _DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    if provider == "gemini" and raw.lower().startswith("gpt"):
        # .env commonly pins SCENARIO_GENERATOR_MODEL/SCENARIO_REVIEW_MODEL to an
        # OpenAI model name. Sending that to Gemini would just 404, so fall back
        # to the Gemini default instead of forcing every switch to also edit
        # those two vars.
        logger.warning(
            "%s=%s looks like an OpenAI model but SCENARIO_LLM_PROVIDER=gemini; "
            "using %s instead. Set %s to a Gemini model name to override.",
            env_name,
            raw,
            default,
            env_name,
        )
        return default
    return raw


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
        # This used to fail silently into a one-line generic prompt, which
        # would quietly drop most of the safety/variety rules with no signal
        # anywhere that it happened. Make it loud instead.
        logger.warning(
            "Could not read scenario guidelines at %s; falling back to a "
            "one-line generic prompt. Scenario safety/variety rules from "
            "scenario_generation_guidelines.md are NOT being applied until "
            "this path is fixed.",
            GUIDELINES_PATH,
        )
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
    # Check every field that can end up in the live system prompt, not just
    # opening_line/persona_name/organization — scenario_summary and
    # conversation_goal are folded into the prompt verbatim (see
    # _to_scenario), so a leak there is just as exploitable as one in the
    # fields the character actually speaks.
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
    if any(_SPOKEN_META.search(value) for value in values):
        raise ValueError("generated scenario breaks character (meta wording found)")
    if any(_REAL_ORGS.search(value) for value in values):
        raise ValueError("generated scenario uses a real institution name")
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
        model=_resolve_model("SCENARIO_REVIEW_MODEL"),
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
- 상대가 방금 한 말에 직접 반응해라. 미리 정해둔 대사를 순서대로 읽지 마라.
- 같은 표현을 반복하지 마라. 매 응답마다 어휘와 문장 구조를 바꿔라.
- "음", "아", "그러니까" 같은 자연스러운 구어체 접속어를 필요할 때 섞어 써라.
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
        tts_style=base.tts_style,
        tts_speed=base.tts_speed,
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
        model=_resolve_model("SCENARIO_GENERATOR_MODEL"),
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
    # Matches the 80-point bar documented in scenario_generation_guidelines.md.
    return review.valid and review.score >= 80


async def generate_scenario(base: Scenario, *, client: AsyncOpenAI | None = None) -> Scenario:
    """Generate, validate, and review a scenario for one outbound call.

    A scenario that fails review (score < 80, or the review call itself
    breaks) is never returned — the attempt is retried with the failure
    reason fed back as a repair hint, so the 80-point gate documented in
    scenario_generation_guidelines.md is actually enforced instead of only
    logged. Callers already fall back to the static safety scenario if every
    attempt is exhausted (see app/services/call_service.get_runtime_scenario).
    """
    # Route through ai.config so a missing key raises the same friendly,
    # actionable error as everywhere else in ai/ ("copy .env.example to
    # .env..."), instead of a bare KeyError that only avoided happening
    # before because some other module happened to import ai.config first
    # and load .env as a side effect.
    openai_client = client or _build_client()
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
            review = await review_scenario(generated, client=openai_client)
            if not _review_is_usable(review):
                details = "; ".join(review.issues) or "score below 80-point bar"
                raise ValueError(
                    f"scenario review rejected (score={review.score}): {details}"
                )
            scenario = _to_scenario(base, generated)
            logger.info(
                "Generated scenario id=%s name=%s score=%s",
                scenario.id,
                scenario.name,
                review.score,
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
