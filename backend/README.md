# ai-challenge-Backend

보이스피싱 대응 훈련 시뮬레이터의 백엔드 API 서버입니다. 참가자에게 AI가 실제 보이스피싱처럼 전화를 걸어 대응 훈련을 진행하고, 통화 내용을 분석해 리포트를 제공합니다.

## 스택

- **FastAPI** (Python 3.13) + **Uvicorn**
- **PostgreSQL** + **SQLAlchemy** + **Alembic** (마이그레이션)
- **ClawOps**: 전화 발신/문자(OTP) 및 통화 웹훅 연동
- **AI 통화 파이프라인**: Deepgram(STT) · ElevenLabs(TTS) · OpenAI/Gemini(LLM) — [`ai/`](../ai) 패키지, `clawops[agent,...]` 의존

## 디렉토리 구조

```
backend/
├── app/
│   ├── main.py            # FastAPI 앱 엔트리포인트, 라우터 등록, CORS 설정
│   ├── database.py        # SQLAlchemy 엔진/세션 (DATABASE_URL 필요)
│   ├── models/            # ORM 모델 (참가자, 동의, 훈련 세션, 통화, 리포트 등)
│   ├── routers/           # API 라우트 (auth, consent, session, call, report, webhook)
│   ├── schemas/           # Pydantic 요청/응답 스키마
│   ├── services/          # 비즈니스 로직 (인증, 통화, 리포트, 스케줄러, SMS 등)
│   ├── training/          # 통화 중 AI 파이프라인 (STT/LLM 세션, 시나리오)
│   └── dependencies/      # 인증 등 FastAPI 의존성
├── alembic/                # DB 마이그레이션
├── tests/                  # pytest 테스트
├── start.sh                # 컨테이너 시작 스크립트 (마이그레이션 재시도 → uvicorn 기동)
├── Dockerfile
└── compose.yaml            # 로컬 개발용 (backend + Postgres)
```

## 주요 API

| Prefix | 태그 | 설명 |
| --- | --- | --- |
| `POST /v1/auth/signup/otp`, `/signup/verify`, `/signup`, `/login` | Auth | SMS OTP 회원가입/로그인 |
| `POST /v1/consents` | Consents | 훈련 참여 동의 제출 |
| `GET /v1/sessions`, `GET /v1/sessions/{id}` | Sessions | 참가자의 훈련 세션 목록/상세 조회 |
| `POST /v1/sessions/{id}/calls` | Calls | 훈련 통화 시작 (ClawOps 발신) |
| `GET /v1/sessions/{id}/report` | Reports | 통화 결과 리포트 조회 |
| `POST /v1/webhooks/clawops/transcript` | ClawOps Webhooks | 통화 종료 후 전사(transcript) 수신 |

## 로컬 실행

```bash
cp .env.example .env   # DATABASE_URL, JWT_SECRET, API 키 등 채우기
docker compose up --build
```

`compose.yaml`은 `backend`(8000번 포트)와 로컬 `db`(Postgres, 5433번 포트) 컨테이너를 함께 띄웁니다. 코드 변경 시 자동 리로드(`UVICORN_RELOAD=1`)됩니다.

## 배포

`start.sh`가 컨테이너 시작 시 `alembic upgrade head`를 최대 10회 재시도한 뒤 `uvicorn`을 기동합니다. 배포 환경(Railway 등)에는 `DATABASE_URL` 또는 `DATABASE_PUBLIC_URL` 환경변수가 반드시 설정되어 있어야 하며, 값이 비어있으면 마이그레이션이 반복 실패합니다.

환경변수 전체 목록과 설명은 [`.env.example`](.env.example)을 참고하세요.
