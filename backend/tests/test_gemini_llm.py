import asyncio

from clawops.agent.pipeline import GeminiLLM

from app.training.gemini_llm import PhoneGeminiLLM


def _install(monkeypatch, script: dict[str, str], calls: list[str]) -> None:
    async def fake_generate(self, messages, tools=None):
        calls.append(self._model)
        behavior = script.get(self._model, "ok")
        if behavior == "fail":
            raise RuntimeError("503 UNAVAILABLE")
        if behavior == "fail_midway":
            yield "앞부분"
            raise RuntimeError("503 mid-stream")
        yield "정상 응답."

    monkeypatch.setattr(GeminiLLM, "generate", fake_generate)


async def _collect(llm: PhoneGeminiLLM) -> tuple[str, Exception | None]:
    out = ""
    try:
        async for token in llm.generate([{"role": "user", "content": "hi"}]):
            out += token
    except Exception as exc:  # noqa: BLE001
        return out, exc
    return out, None


def test_no_retry_needed_calls_primary_once(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, {}, calls)
    llm = PhoneGeminiLLM(
        api_key="k", model="primary", fallback_models=("backup",),
        attempts=2, retry_delay=0,
    )
    out, err = asyncio.run(_collect(llm))
    assert out == "정상 응답."
    assert err is None
    assert calls == ["primary"]


def test_primary_exhausts_retries_then_falls_back_to_next_model(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, {"primary": "fail"}, calls)
    llm = PhoneGeminiLLM(
        api_key="k", model="primary", fallback_models=("backup",),
        attempts=2, retry_delay=0,
    )
    out, err = asyncio.run(_collect(llm))
    assert out == "정상 응답."
    assert err is None
    assert calls == ["primary", "primary", "backup"]


def test_every_model_failing_raises(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, {"primary": "fail", "backup": "fail"}, calls)
    llm = PhoneGeminiLLM(
        api_key="k", model="primary", fallback_models=("backup",),
        attempts=2, retry_delay=0,
    )
    out, err = asyncio.run(_collect(llm))
    assert out == ""
    assert isinstance(err, RuntimeError)
    assert calls == ["primary", "primary", "backup", "backup"]


def test_midstream_failure_keeps_partial_reply(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, {"primary": "fail_midway"}, calls)
    llm = PhoneGeminiLLM(
        api_key="k", model="primary", fallback_models=("backup",),
        attempts=2, retry_delay=0,
    )
    out, err = asyncio.run(_collect(llm))
    assert out == "앞부분"
    assert err is None
    assert calls == ["primary"]  # no retry once speech has started


def test_same_model_as_primary_is_not_duplicated_as_a_fallback(monkeypatch):
    calls: list[str] = []
    _install(monkeypatch, {"primary": "fail"}, calls)
    llm = PhoneGeminiLLM(
        api_key="k", model="primary", fallback_models=("primary", "backup"),
        attempts=1, retry_delay=0,
    )
    out, err = asyncio.run(_collect(llm))
    assert out == "정상 응답."
    assert calls == ["primary", "backup"]
