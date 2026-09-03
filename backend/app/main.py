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
load_dotenv(_BACKEND_DIR / ".env", override=True)
for _ai_env in (_REPO_DIR / "ai" / ".env", Path("/packages/ai/.env")):
    load_dotenv(_ai_env, override=False)
logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(message)s")
logging.getLogger("clawops.agent").setLevel(logging.INFO)
logging.getLogger(__name__).info(
    "Signup SMS: ClawOps (%s) · call AI: PipelineSession (%s)",
    "configured"
    if all(
        os.getenv(name)
        for name in ("CLAWOPS_API_KEY", "CLAWOPS_ACCOUNT_ID", "CLAWOPS_SMS_FROM")
    )
    else "configuration missing",
    os.getenv("CALL_SCENARIO", "").strip() or "고정 시나리오 무작위 선택",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.services.training_scheduler import (
        start_training_scheduler,
        stop_training_scheduler,
    )

    start_training_scheduler()
    try:
        yield
    finally:
        stop_training_scheduler()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "http://localhost:3001",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:3001",
    ],
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
