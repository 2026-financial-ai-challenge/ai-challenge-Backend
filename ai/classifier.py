"""Post-call behavior classifier.

Scores are intentionally not computed here. This module only labels risk and
defense behaviors with evidence spans for a backend rule engine.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from ai.config import openai_api_key, openai_model
from ai.llm_stream import ChatMessage

RISK_LABELS = (
    "개인정보 제공",
    "금융정보 제공",
    "상대방 기관명 신뢰",
    "송금 의사 표현",
    "링크 접근 의사",
    "앱 설치 의사",
    "통화 장시간 지속",
)

DEFENSE_LABELS = (
    "상대방 신원 확인",
    "공식 대표번호 확인 의사",
    "개인정보 제공 거절",
    "송금 거절",
    "전화 종료(빠른 판단)",
    "신고 의사 표현",
)

_SYSTEM = f"""
너는 보이스피싱 대응 훈련의 행동 분류기다. 점수나 등급은 매기지 마라.
대화 전체만 보고 관찰된 행동을 아래 라벨 중에서만 고른다.

위험행동 라벨:
- {", ".join(RISK_LABELS)}

방어행동 라벨:
- {", ".join(DEFENSE_LABELS)}

규칙:
- 반드시 JSON 객체만 반환한다. 다른 텍스트를 붙이지 마라.
- 형식:
  {{
    "risk_behaviors": [{{"label": "...", "evidence": "..."}}],
    "defense_behaviors": [{{"label": "...", "evidence": "..."}}]
  }}
- label은 위 목록에 있는 문자열만 사용한다.
- evidence는 대화에서 근거가 된 짧은 인용이다. 없으면 그 라벨을 넣지 마라.
- 실제로 보인 행동만 넣는다. 추측으로 채우지 마라.
- "통화 장시간 지속"은 사용자 발화가 5턴 이상이거나 상대 요구를 반복 수용하며 통화를 이어간 경우에만 넣는다.
- 이 대화는 사전 동의 하의 교육 시뮬레이션이다.
""".strip()


class BehaviorItem(BaseModel):
    label: str
    evidence: str = ""


class ClassificationResult(BaseModel):
    risk_behaviors: list[BehaviorItem] = Field(default_factory=list)
    defense_behaviors: list[BehaviorItem] = Field(default_factory=list)


def format_transcript(history: Sequence[ChatMessage]) -> str:
    lines: list[str] = []
    for message in history:
        role = message.get("role", "")
        if role not in {"user", "assistant"}:
            continue
        speaker = "사용자" if role == "user" else "상대"
        content = (message.get("content") or "").strip()
        if content:
            lines.append(f"[{speaker}] {content}")
    return "\n".join(lines)


def _filter_known_labels(result: ClassificationResult) -> ClassificationResult:
    def keep(item: BehaviorItem, allowed: tuple[str, ...]) -> bool:
        return item.label in allowed and bool(item.evidence.strip())

    return ClassificationResult(
        risk_behaviors=[item for item in result.risk_behaviors if keep(item, RISK_LABELS)],
        defense_behaviors=[
            item for item in result.defense_behaviors if keep(item, DEFENSE_LABELS)
        ],
    )


async def classify_behaviors(
    history: Sequence[ChatMessage],
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Return the exact JSON payload expected by the backend rule engine."""
    transcript = format_transcript(history)
    user_turns = sum(1 for message in history if message.get("role") == "user")
    openai_client = client or AsyncOpenAI(api_key=openai_api_key())

    response = await openai_client.chat.completions.create(
        model=model or openai_model(),
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"사용자 발화 턴 수: {user_turns}\n\n"
                    f"대화 기록:\n{transcript or '(대화 없음)'}"
                ),
            },
        ],
    )

    raw = response.choices[0].message.content or "{}"
    try:
        parsed = ClassificationResult.model_validate(json.loads(raw))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise RuntimeError(f"Classifier returned invalid JSON: {raw[:500]}") from exc

    return _filter_known_labels(parsed).model_dump()
