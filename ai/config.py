from pathlib import Path

from dotenv import load_dotenv

# 환경변수는 backend/.env 하나만 쓴다. ai/.env는 만들지 않는다.
load_dotenv(Path(__file__).resolve().parent.parent / "backend" / ".env")

# 리포트 채점이 Gemini를 OpenAI 클라이언트로 부를 때 쓰는 주소
GEMINI_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
