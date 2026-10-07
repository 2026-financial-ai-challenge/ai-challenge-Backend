# backend — FastAPI 서버

보이스피싱 대응 훈련 서비스의 API 서버입니다. 참가자 가입·동의를 받고, ClawOps 매니지드 에이전트로 훈련 전화를 걸고, 통화 녹취록을 채점해 리포트를 제공합니다.
구조와 흐름은 [`../docs/architecture.md`](../docs/architecture.md), API는 [`../docs/api-spec.md`](../docs/api-spec.md)를 참고하세요.

## 스택

- **FastAPI** (Python 3.13) + **Uvicorn**
- **PostgreSQL** + **SQLAlchemy** + **Alembic** (마이그레이션)
- **Redis**: 가입 인증(OTP·재발송 대기·인증 토큰) 상태
- **ClawOps**: 매니지드 음성 에이전트 발신, 가입 인증 문자, 통화 상태·녹취록 웹훅
- **Gemini / OpenAI**: 통화 녹취록 채점(리포트)
- 시나리오와 에이전트 설정은 [`ai/`](../ai) 패키지를 사용합니다.

## 통화 흐름

1. 참가자가 동의하면 세션을 만들고 ClawOps 매니지드 에이전트로 발신합니다. 시나리오는 통화마다 지시문(CallContext)으로 보내고, 대화는 ClawOps 서버가 진행합니다.
2. 상태 웹훅으로 통화 연결·종료를 받아 세션 상태를 바꿉니다.
3. 통화가 끝나면 녹취록을 요청하고, 녹취록 웹훅이 오면 AI와 참가자의 발화를 나눠 채점한 리포트를 저장합니다.
4. 불시 전화에 동의했다면 1차 통화 뒤 30~60분 사이 무작위 시점에 다른 번호로 한 번 더 겁니다. 회차 리포트는 1차·불시·최종(두 통화 비교)으로 구성됩니다.

## 디렉토리 구조

```
backend/
├── app/
│   ├── main.py            # FastAPI 앱, 라우터 등록, CORS, 백그라운드 작업 기동
│   ├── database.py        # SQLAlchemy 엔진/세션 (DATABASE_URL 필요)
│   ├── models/            # ORM 모델 (참가자, 동의, 훈련 세션, 통화, 녹취, 리포트, 불시 전화 예약)
│   ├── routers/           # API 라우트 (auth, consent, session, call, report, webhook)
│   ├── schemas/           # Pydantic 요청/응답 스키마
│   ├── services/          # 비즈니스 로직 (인증, 통화, 리포트, 불시 전화 스케줄러, 개인정보 파기, SMS)
│   ├── training/          # ai 패키지 연결과 통화 시나리오 선택
│   ├── periodic.py        # 백그라운드 주기 작업 실행기
│   ├── phone_verification_store.py  # 가입 인증 상태 (Redis)
│   └── dependencies/      # 인증 등 FastAPI 의존성
├── alembic/               # DB 마이그레이션
├── tests/                 # pytest 테스트
├── start.sh               # 컨테이너 시작 스크립트 (마이그레이션 재시도 → uvicorn 기동)
├── Dockerfile
├── compose.yaml           # 로컬 개발용 (backend + Postgres + Redis)
├── compose.prod.yaml      # 서버 배포용 (+ Caddy HTTPS)
└── Caddyfile
```

## 로컬 실행

```bash
cp .env.example .env   # JWT_SECRET, ClawOps 키, Gemini 키 등 채우기
docker compose up --build
```

`compose.yaml`은 `backend`(8000번 포트), `redis`(6379), 로컬 `db`(Postgres, 5433번 포트) 컨테이너를 함께 띄우고, 코드가 바뀌면 자동으로 다시 읽습니다(`UVICORN_RELOAD=1`). Swagger UI는 `http://localhost:8000/docs`입니다.

ClawOps 웹훅을 로컬에서 받으려면 ngrok 같은 터널 주소를 `PUBLIC_BASE_URL`에 넣습니다. 비어 있으면 통화 상태를 받지 못해 세션이 `calling`에 머물고 불시 전화도 예약되지 않습니다.

## 테스트

```bash
pytest
```

`DATABASE_URL`이 가리키는 DB 이름 뒤에 `_test`를 붙인 별도 DB를 만들어 마이그레이션한 뒤 실행합니다. `DATABASE_URL`이 로컬 주소가 아니면 실행을 거부하므로, 그때는 `TEST_DATABASE_URL`을 직접 지정합니다.

## 배포

AWS Lightsail **서울** 리전의 Ubuntu 서버 한 대에 `compose.prod.yaml`로 올립니다. ClawOps가 국외 IP의 문자 발송을 거부하므로 리전은 반드시 한국이어야 합니다.

`compose.prod.yaml`은 개발용과 달리 코드 리로드가 없고, DB·Redis 포트를 밖으로 열지 않으며, Caddy가 HTTPS를 맡습니다. `DATABASE_URL`·`REDIS_URL`은 compose가 채우므로 `.env`에 넣지 않습니다.

1. Lightsail 방화벽에서 22·80·443을 열고 고정 IP를 연결합니다. 도메인은 IP의 점을 하이픈으로 바꾼 `<IP>.sslip.io`를 씁니다.
2. 서버에 Docker를 설치하고 저장소를 받습니다.
3. `backend/.env`를 만들고, `.env.example` 항목에 더해 아래를 채웁니다.

   ```env
   DOMAIN=<IP 하이픈>.sslip.io
   PUBLIC_BASE_URL=https://<IP 하이픈>.sslip.io
   LOCAL_DB_PASSWORD=<openssl rand -hex 16>
   ```

4. 실행합니다. 업데이트할 때도 같은 명령을 씁니다.

   ```bash
   git pull && docker compose -f compose.prod.yaml up -d --build
   ```

처음 한 번은 직접 실행하고, 이후에는 `develop`에 push될 때마다 GitHub Actions([`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml))가 서버에 SSH로 들어가 같은 명령을 실행합니다. 저장소 Secrets에 `LIGHTSAIL_HOST`·`LIGHTSAIL_USER`·`LIGHTSAIL_SSH_KEY`·`DEPLOY_PATH`를 등록해야 하고, 서버의 저장소는 `develop` 브랜치를 받아 두어야 합니다.

`.env`를 고친 뒤에는 `docker compose -f compose.prod.yaml up -d`로 컨테이너를 다시 만들어야 반영됩니다(`restart`로는 다시 읽지 않습니다).

배포 후 ClawOps 콘솔의 녹취록 웹훅은 `https://<DOMAIN>/v1/webhooks/clawops/transcript`로, 프론트엔드(Vercel)의 `NEXT_PUBLIC_API_BASE_URL`은 `https://<DOMAIN>`으로 맞춥니다.

## 환경변수

전체 목록과 설명은 [`.env.example`](.env.example)에 있습니다. `backend/.env` 하나만 씁니다(`ai/.env`는 만들지 않습니다).

| 변수 | 용도 |
| --- | --- |
| `JWT_SECRET` | 로그인 토큰 서명. 비우면 개발용 기본값을 쓰므로 운영에서는 반드시 설정 |
| `CLAWOPS_API_KEY` / `CLAWOPS_ACCOUNT_ID` | 발신, 녹취록 조회, 문자 발송 |
| `CLAWOPS_PHONE_NUMBER` / `CLAWOPS_UNANNOUNCED_PHONE_NUMBER` | 1차 / 불시 전화 발신번호 |
| `CLAWOPS_SMS_FROM` | 가입 인증 문자 발신번호 (등록된 070 번호) |
| `CLAWOPS_WEBHOOK_SIGNING_SECRET` | 웹훅 서명 검증 |
| `PUBLIC_BASE_URL` | ClawOps가 통화 상태를 보낼 공개 주소 |
| `CALL_AGENT_VARIANT` | 통화 에이전트 방식 `external_tts`(기본) / `live` / `ab` |
| `GEMINI_API_KEY` / `OPENAI_API_KEY` | 리포트 채점 LLM (둘 다 없으면 키워드 기반 간이 채점) |
| `UNANNOUNCED_CALL_*` | 불시 전화 간격(기본 1800~3600초)과 재시도 |
| `PARTICIPANT_RETENTION_DAYS` | 개인정보 보존 기간 (기본 30일) |
