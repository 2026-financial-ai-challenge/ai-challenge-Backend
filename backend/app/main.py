import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.errors import ApiError
from app.routers import auth, call, consent, report, session, webhook

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_DIR = Path(__file__).resolve().parents[2]
# 이미 환경에 있는 값(compose env_file, 테스트용 DB 주소)은 덮어쓰지 않는다.
for _env_file in (
    _BACKEND_DIR / ".env",
    _REPO_DIR / "ai" / ".env",
    Path("/packages/ai/.env"),
):
    load_dotenv(_env_file, override=False)
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")


logging.getLogger(__name__).info(
    "ClawOps SMS: %s · call scenario: %s",
    "configured"
    if all(
        os.getenv(name)
        for name in ("CLAWOPS_API_KEY", "CLAWOPS_ACCOUNT_ID", "CLAWOPS_SMS_FROM")
    )
    else "configuration missing",
    os.getenv("CALL_SCENARIO", "").strip() or "random",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.services.data_retention import retention_worker
    from app.services.training_scheduler import training_scheduler

    workers = (training_scheduler, retention_worker)
    for worker in workers:
        worker.start()
    try:
        yield
    finally:
        for worker in workers:
            worker.stop()


app = FastAPI(lifespan=lifespan)

_ALLOWED_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "https://safety-phishing-call.vercel.app",
    "https://gaoncs.vercel.app",
)
# Vercel 미리보기 배포는 매번 하위 도메인이 달라 패턴으로 허용한다.
_PREVIEW_ORIGIN_PATTERN = r"https://safety-phishing-call-[a-z0-9-]+\.vercel\.app"


def _allowed_origins() -> list[str]:
    """기본 목록에 CORS_ALLOWED_ORIGINS를 더한다. Origin 헤더에는 끝 슬래시가 없어 지운다."""
    extra = os.getenv("CORS_ALLOWED_ORIGINS", "")
    return list(_ALLOWED_ORIGINS) + [
        origin.strip().rstrip("/") for origin in extra.split(",") if origin.strip()
    ]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins(),
    allow_origin_regex=_PREVIEW_ORIGIN_PATTERN,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(ApiError)
def handle_api_error(_request: Request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"message": exc.message, "code": exc.code},
    )


@app.get("/")
def root():
    return {"message": "Phishing Call Backend API"}


app.include_router(consent.router)
app.include_router(auth.router)
app.include_router(session.router)
app.include_router(call.router)
app.include_router(report.router)
app.include_router(webhook.router)
