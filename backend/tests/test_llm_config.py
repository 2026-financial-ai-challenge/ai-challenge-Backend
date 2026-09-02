import os

from app.training.llm_config import (
    GEMINI_OPENAI_BASE_URL,
    LlmConfigurationError,
    apply_clawops_openai_compat_env,
    llm_settings,
)


def test_openai_settings_require_api_key(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        llm_settings()
        raise AssertionError("expected LlmConfigurationError")
    except LlmConfigurationError as exc:
        assert "OPENAI_API_KEY" in str(exc)


def test_gemini_settings_use_compat_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)

    settings = llm_settings()
    assert settings.provider == "gemini"
    assert settings.api_key == "gemini-test-key"
    assert settings.model == "gemini-2.5-flash"
    assert settings.base_url == GEMINI_OPENAI_BASE_URL


def test_apply_clawops_sets_openai_base_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash-lite")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    settings = apply_clawops_openai_compat_env()
    assert settings.model == "gemini-2.5-flash-lite"
    assert os.environ["OPENAI_BASE_URL"] == GEMINI_OPENAI_BASE_URL


def test_build_pipeline_session_uses_gemini_model(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "testvoice123")
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.5-flash")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    from app.services import call_service
    from app.training.scenarios import get_call_scenario

    session = call_service.build_pipeline_session(get_call_scenario())
    assert session._llm.model == "gemini-2.5-flash"
    assert os.environ["OPENAI_BASE_URL"] == GEMINI_OPENAI_BASE_URL
