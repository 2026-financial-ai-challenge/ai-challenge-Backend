"""LLM provider for phone calls, scenario generation, and reports.

Default is OpenAI. Set LLM_PROVIDER=gemini and GEMINI_API_KEY to use
Google's free-tier Gemini API through the OpenAI-compatible endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class LlmConfigurationError(Exception):
    pass


@dataclass(frozen=True)
class LlmSettings:
    provider: str
    api_key: str
    model: str
    base_url: str | None


def llm_provider() -> str:
    raw = os.getenv("LLM_PROVIDER", "openai").strip().lower()
    if raw in {"gemini", "google", "google-gemini"}:
        return "gemini"
    return "openai"


def llm_settings() -> LlmSettings:
    if llm_provider() == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise LlmConfigurationError("GEMINI_API_KEY is not set")
        model = os.getenv("GEMINI_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
        return LlmSettings(
            provider="gemini",
            api_key=api_key,
            model=model,
            base_url=GEMINI_OPENAI_BASE_URL,
        )

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise LlmConfigurationError("OPENAI_API_KEY is not set")
    model = os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip() or DEFAULT_OPENAI_MODEL
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None
    return LlmSettings(
        provider="openai",
        api_key=api_key,
        model=model,
        base_url=base_url,
    )


def apply_clawops_openai_compat_env(settings: LlmSettings | None = None) -> LlmSettings:
    """ClawOps OpenAILLM only passes api_key; the SDK still reads OPENAI_BASE_URL."""
    resolved = settings or llm_settings()
    if resolved.base_url:
        os.environ["OPENAI_BASE_URL"] = resolved.base_url
    elif os.environ.get("OPENAI_BASE_URL") == GEMINI_OPENAI_BASE_URL:
        os.environ.pop("OPENAI_BASE_URL", None)
    return resolved


def chat_client(settings: LlmSettings | None = None):
    from openai import AsyncOpenAI

    resolved = settings or llm_settings()
    kwargs: dict[str, str] = {"api_key": resolved.api_key}
    if resolved.base_url:
        kwargs["base_url"] = resolved.base_url
    return AsyncOpenAI(**kwargs)
