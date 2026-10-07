# ai-challenge-Backend

보이스피싱 대응 훈련 시뮬레이터의 백엔드 API 서버입니다. 참가자에게 AI가 실제 보이스피싱처럼 전화를 걸어 대응 훈련을 진행하고, 통화 내용을 분석해 리포트를 제공합니다.

## 스택

- **FastAPI** (Python 3.13) + **Uvicorn**
- **PostgreSQL** + **SQLAlchemy** + **Alembic** (마이그레이션)
- **Redis**: 가입 인증(OTP) 상태
- **ClawOps**: 매니지드 음성 에이전트 발신, 문자(OTP·훈련 링크), 통화 상태·녹취록 웹훅
- **OpenAI / Gemini**: 통화 녹취록 채점(리포트)
- 시나리오와 에이전트 설정은 [`ai/`](../ai) 패키지를 사용합니다.

## 통화 흐름

1. 동의한 참가자에게 ClawOps 매니지드 에이전트로 발신합니다. 대화는 ClawOps 서버가 진행합니다.
2. 상태 웹훅으로 통화 연결·종료를 받습니다. 사건 조회 시나리오는 연결 후 정해진 시간에 훈련 링크 문자를 보냅니다.
3. 통화가 끝나면 녹취록을 요청하고, 녹취록 웹훅이 오면 화자를 나눠 채점한 리포트를 저장합니다.
4. 1차 통화 뒤 30~60분 사이 무작위 시점에 불시 전화를 겁니다. 회차 리포트는 1차·불시·최종(두 통화 비교)으로 구성됩니다.

이전에 쓰던 서버 내 음성 처리 방식(pipeline/realtime)은 [`docs/legacy-call-paths.md`](docs/legacy-call-paths.md)에 정리했습니다.

## 디렉토리 구조

```
backend/
├── app/
│   ├── main.py            # FastAPI 앱 엔트리포인트, 라우터 등록, CORS 설정
│   ├── database.py        # SQLAlchemy 엔진/세션 (DATABASE_URL 필요)
│   ├── models/            # ORM 모델 (참가자, 동의, 훈련 세션, 통화, 리포트 등)
│   ├── routers/           # API 라우트 (auth, consent, session, call, report, web_training, webhook)
│   ├── schemas/           # Pydantic 요청/응답 스키마
│   ├── services/          # 비즈니스 로직 (인증, 통화, 리포트, 스케줄러, SMS 등)
│   ├── training/          # ai 패키지 연결과 통화 시나리오 선택
│   ├── periodic.py        # 백그라운드 주기 작업 (불시 전화 스케줄러, 개인정보 파기)
│   └── dependencies/      # 인증 등 FastAPI 의존성
├── alembic/                # DB 마이그레이션
├── docs/                   # 이전 통화 방식 기록
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
| `POST /v1/web-training/sessions/{id}/link`, `GET /v1/web-training/{token}`, `POST /v1/web-training/{token}/events` | WebTraining | 문자 링크 발급·확인·행동 기록 |
| `POST /v1/webhooks/clawops/status` | ClawOps Webhooks | 통화 상태(연결·종료) 수신 |
| `POST /v1/webhooks/clawops/transcript` | ClawOps Webhooks | 통화 종료 후 녹취록 수신 |

## 로컬 실행

```bash
cp .env.example .env   # DATABASE_URL, JWT_SECRET, API 키 등 채우기
docker compose up --build
```

`compose.yaml`은 `backend`(8000번 포트), `redis`, 로컬 `db`(Postgres, 5433번 포트) 컨테이너를 함께 띄웁니다. 코드 변경 시 자동 리로드(`UVICORN_RELOAD=1`)됩니다.

## 배포

AWS Lightsail **서울** 리전의 Ubuntu 서버 한 대에 `compose.prod.yaml`로 올립니다. ClawOps가 국외 IP의 문자 발송을 거부하므로 리전은 반드시 한국이어야 합니다.

`compose.prod.yaml`은 개발용 `compose.yaml`과 달리 코드 리로드가 없고, DB·Redis 포트를 밖으로 열지 않으며, Caddy가 HTTPS를 맡습니다. `DATABASE_URL`·`REDIS_URL`은 compose가 채우므로 `.env`에 넣지 않습니다.

1. Lightsail 방화벽에서 22·80·443을 열고 고정 IP를 연결합니다. 도메인은 IP의 점을 하이픈으로 바꾼 `<IP>.sslip.io`를 씁니다.
2. 서버에 Docker를 설치하고 저장소를 받습니다.
3. `backend/.env`를 만들고, `.env.example` 항목에 더해 아래를 채웁니다.

   ```env
   DOMAIN=54-116-186-165.sslip.io
   PUBLIC_BASE_URL=https://54-116-186-165.sslip.io
   LOCAL_DB_PASSWORD=<openssl rand -hex 16>
   ```

4. 실행합니다. 업데이트할 때도 같은 명령을 씁니다.

   ```bash
   git pull && docker compose -f compose.prod.yaml up -d --build
   ```

처음 한 번은 직접 실행하고, 이후에는 `develop`에 push될 때마다 GitHub Actions([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml))가 서버에 SSH로 들어가 같은 명령을 실행합니다. 저장소 Secrets에 `LIGHTSAIL_HOST`·`LIGHTSAIL_USER`·`LIGHTSAIL_SSH_KEY`·`DEPLOY_PATH`를 등록해야 하고, 서버의 저장소는 `develop` 브랜치를 받아 두어야 합니다.

`start.sh`가 컨테이너 시작 시 `alembic upgrade head`를 최대 10회 재시도한 뒤 `uvicorn`을 기동합니다.

배포 후 ClawOps 콘솔의 전사 웹훅은 `https://<DOMAIN>/v1/webhooks/clawops/transcript`로, 프론트엔드(Vercel)의 `NEXT_PUBLIC_API_BASE_URL`은 `https://<DOMAIN>`으로 맞춥니다.

환경변수 전체 목록과 설명은 [`.env.example`](.env.example)을 참고하세요.
