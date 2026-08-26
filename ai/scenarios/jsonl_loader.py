from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ai.safety import SAFETY_RULES
from ai.scenarios.types import Scenario
from ai.voices import Onyu


DATASET_PATH = Path(__file__).with_name("voice_phishing_training_data.jsonl")


def load_jsonl_scenarios(path: Path = DATASET_PATH) -> dict[str, Scenario]:
    scenarios: dict[str, Scenario] = {}
    with path.open(encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
                scenario = _to_scenario(record)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid scenario record at {path.name}:{line_number}: {exc}"
                ) from exc
            if scenario.id in scenarios:
                raise ValueError(f"Duplicate scenario id '{scenario.id}' in {path.name}")
            scenarios[scenario.id] = scenario
    return scenarios


def _to_scenario(record: dict[str, Any]) -> Scenario:
    messages = record["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a non-empty list")
    if any(not isinstance(message, dict) for message in messages):
        raise ValueError("each messages item must be an object")
    if any(message.get("role") not in {"system", "assistant", "user"} for message in messages):
        raise ValueError("messages role must be system, assistant, or user")

    system_messages = _contents(messages, "system")
    assistant_messages = _contents(messages, "assistant")
    if not system_messages or not assistant_messages:
        raise ValueError("messages must contain system and assistant roles")

    tactics = _string_tuple(record["tactics"], "tactics")
    red_flags = _string_tuple(record["red_flags"], "red_flags")
    ideal_response = _required_string(
        record["ideal_trainee_response"], "ideal_trainee_response"
    )
    difficulty = _required_string(record.get("difficulty", "중"), "difficulty")

    example_lines = []
    for message in messages[1:]:
        role = message.get("role")
        content = _required_string(message.get("content"), "messages.content")
        if role == "assistant":
            example_lines.append(f"상대: {content}")
        elif role == "user":
            example_lines.append(f"훈련자: {content}")

    system_prompt = "\n\n".join(
        (
            SAFETY_RULES,
            system_messages[0],
            "[이번 시나리오에서 사용할 심리 기법]\n- " + "\n- ".join(tactics),
            "[대화 흐름 예시]\n" + "\n".join(example_lines),
        )
    )
    max_turns = {"하": 6, "중": 8, "상": 10}.get(difficulty, 8)

    return Scenario(
        id=_required_string(record["id"], "id"),
        name=_required_string(record["type"], "type"),
        subtype=_required_string(record["subtype"], "subtype"),
        difficulty=difficulty,
        opening_line=assistant_messages[0],
        system_prompt=system_prompt,
        max_turns=max_turns,
        tts_voice_id=Onyu,
        tactics=tactics,
        red_flags=red_flags,
        ideal_trainee_response=ideal_response,
    )


def _contents(messages: list[dict[str, Any]], role: str) -> list[str]:
    return [
        _required_string(message.get("content"), "messages.content")
        for message in messages
        if message.get("role") == role
    ]


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty list")
    return tuple(_required_string(item, field) for item in value)


def _required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
