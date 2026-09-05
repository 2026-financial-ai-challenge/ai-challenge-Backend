import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.training.fallback_llm import FallbackLLM


class _FakeLLM:
    """A generate()-shaped stand-in. `behavior` drives what one call does."""

    def __init__(self, model: str, behavior: str) -> None:
        self.model = model
        self.provider = model
        self._max_tokens = 99
        self.behavior = behavior
        self.calls = 0

    async def generate(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[str]:
        self.calls += 1
        if self.behavior == "fail":
            raise RuntimeError("boom")
        if self.behavior == "fail_midway":
            yield "앞부분"
            raise RuntimeError("mid-stream boom")
        yield "정상 응답."


async def _collect(llm: FallbackLLM) -> tuple[str, Exception | None]:
    out = ""
    try:
        async for token in llm.generate([{"role": "user", "content": "hi"}]):
            out += token
    except Exception as exc:  # noqa: BLE001 - test wants the raised type
        return out, exc
    return out, None


def test_primary_success_never_touches_secondary():
    primary, secondary = _FakeLLM("p", "ok"), _FakeLLM("s", "ok")
    llm = FallbackLLM(primary, secondary, primary_label="P", secondary_label="S")
    out, err = asyncio.run(_collect(llm))
    assert out == "정상 응답."
    assert err is None
    assert primary.calls == 1
    assert secondary.calls == 0


def test_primary_failure_before_any_token_falls_back():
    primary, secondary = _FakeLLM("p", "fail"), _FakeLLM("s", "ok")
    llm = FallbackLLM(primary, secondary, primary_label="P", secondary_label="S")
    out, err = asyncio.run(_collect(llm))
    assert out == "정상 응답."
    assert err is None
    assert primary.calls == 1
    assert secondary.calls == 1


def test_both_providers_failing_raises():
    primary, secondary = _FakeLLM("p", "fail"), _FakeLLM("s", "fail")
    llm = FallbackLLM(primary, secondary, primary_label="P", secondary_label="S")
    out, err = asyncio.run(_collect(llm))
    assert out == ""
    assert isinstance(err, RuntimeError)


def test_midstream_failure_keeps_partial_reply_and_does_not_fall_back():
    """Switching after speech has started would repeat words out loud."""
    primary, secondary = _FakeLLM("p", "fail_midway"), _FakeLLM("s", "ok")
    llm = FallbackLLM(primary, secondary, primary_label="P", secondary_label="S")
    out, err = asyncio.run(_collect(llm))
    assert out == "앞부분"
    assert err is None
    assert secondary.calls == 0


def test_exposes_primary_model_provider_and_max_tokens():
    primary, secondary = _FakeLLM("primary-model", "ok"), _FakeLLM("secondary-model", "ok")
    llm = FallbackLLM(primary, secondary, primary_label="P", secondary_label="S")
    assert llm.model == "primary-model"
    assert llm.provider == "primary-model"
    assert llm._max_tokens == 99
