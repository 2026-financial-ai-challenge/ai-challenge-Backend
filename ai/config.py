"""backend/.env를 읽는다. ai/.env가 있으면 그 값이 먼저다(만들지 않는다)."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_AI_DIR = Path(__file__).resolve().parent
load_dotenv(_AI_DIR / ".env")
load_dotenv(_AI_DIR.parent / "backend" / ".env", override=False)

# Gemini의 OpenAI 호환 엔드포인트. 리포트 채점에 쓴다.
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
