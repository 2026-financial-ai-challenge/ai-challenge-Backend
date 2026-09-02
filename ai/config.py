from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_AI_DIR = Path(__file__).resolve().parent
load_dotenv(_AI_DIR / ".env")


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is not set. Copy ai/.env.example to ai/.env and fill in the keys."
        )
    return value


def openai_api_key() -> str:
    return _require("OPENAI_API_KEY")


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


# Gemini's OpenAI-compatible endpoint: https://ai.google.dev/gemini-api/docs/openai
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def scenario_llm_provider() -> str:
    """Which LLM backend generates/reviews dynamic call scenarios.

    'openai' (default) or 'gemini'. Lets a quota-exhausted OpenAI key be
    swapped for a Gemini free-tier key without touching scenario generation
    code — Gemini is queried through its OpenAI-compatible endpoint, so the
    same AsyncOpenAI client works for both.
    """
    return os.getenv("SCENARIO_LLM_PROVIDER", "openai").strip().lower() or "openai"


def gemini_api_key() -> str:
    return _require("GEMINI_API_KEY")


def elevenlabs_api_key() -> str:
    return _require("ELEVENLABS_API_KEY")


def elevenlabs_voice_id() -> str:
    return _require("ELEVENLABS_VOICE_ID")


def elevenlabs_model_id() -> str:
    return os.getenv("ELEVENLABS_MODEL_ID", "eleven_flash_v2_5").strip() or "eleven_flash_v2_5"


def elevenlabs_output_format() -> str:
    return os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_24000").strip() or "pcm_24000"


def elevenlabs_sample_rate() -> int:
    return int(os.getenv("ELEVENLABS_SAMPLE_RATE", "24000"))


def deepgram_api_key() -> str:
    return _require("DEEPGRAM_API_KEY")


def deepgram_model() -> str:
    return os.getenv("DEEPGRAM_MODEL", "nova-2").strip() or "nova-2"


def stt_language() -> str:
    return os.getenv("DEEPGRAM_LANGUAGE", "ko").strip() or "ko"


def stt_sample_rate() -> int:
    return int(os.getenv("STT_SAMPLE_RATE", "16000"))


def stt_endpointing_ms() -> int:
    return int(os.getenv("STT_ENDPOINTING_MS", "400"))
