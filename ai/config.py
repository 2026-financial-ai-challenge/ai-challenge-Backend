from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_AI_DIR = Path(__file__).resolve().parent
load_dotenv(_AI_DIR / ".env")
# backend/.env is the single source of truth for the in-call LLM
# (CALL_LLM_PROVIDER, GEMINI_API_KEY, GEMINI_MODEL). Filling the gaps from it
# means the local latency harness measures the same provider the phone
# pipeline uses without those keys being pasted into two files, where they
# would inevitably drift. override=False keeps ai/.env authoritative for
# anything it does set, and mirrors how backend/app/main.py reads ai/.env.
load_dotenv(_AI_DIR.parent / "backend" / ".env", override=False)


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


def call_llm_provider() -> str:
    """Which LLM answers the trainee during a live turn.

    Mirrors backend/.env's CALL_LLM_PROVIDER. Without this the local latency
    harness always measured OpenAI even when the phone pipeline was running
    on Gemini, so its numbers described a path nobody was using.
    """
    return os.getenv("CALL_LLM_PROVIDER", "openai").strip().lower() or "openai"


def call_llm_model() -> str:
    if call_llm_provider() == "gemini":
        return os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip() or "gemini-3.5-flash-lite"
    return openai_model()


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
    """Silence before Deepgram calls a turn finished.

    This is what actually floors perceived response latency: a normal turn
    ends on speech_final, which fires after this much silence.
    """
    return int(os.getenv("STT_ENDPOINTING_MS", "400"))


def stt_utterance_end_ms() -> int:
    """Backstop for turns where Deepgram never sends speech_final."""
    return int(os.getenv("STT_UTTERANCE_END_MS", "1000"))
