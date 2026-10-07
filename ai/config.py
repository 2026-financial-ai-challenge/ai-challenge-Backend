"""backend/.env 로딩. ai/.env가 있으면 그쪽이 우선이니 만들지 말 것."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_AI_DIR = Path(__file__).resolve().parent
load_dotenv(_AI_DIR / ".env")
load_dotenv(_AI_DIR.parent / "backend" / ".env", override=False)

# 리포트 채점용 Gemini OpenAI 호환 엔드포인트
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
