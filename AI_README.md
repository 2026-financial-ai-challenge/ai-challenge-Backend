# `AI` — 시나리오 엔진 · 안전 규칙 · 음성 파이프라인

보이스피싱 훈련 통화에서 **AI가 무엇을 말할지 결정하는 부분**을 담는 패키지입니다.
FastAPI 서버(`backend/app`)와 로컬 개발 하네스가 이 패키지를 **공유해서** 씁니다.

## 왜 `backend` 바깥에 있나

시나리오·안전 규칙·행동 라벨·통화 종료 판정은 두 진입점이 **똑같이** 봐야 하는 정의입니다.

- 전화 경로: `backend/app/services/call_service.py` → `backend/app/training/pipeline_session.py`
- 로컬 경로: `python -m ai.test_latency --mic` (마이크·스피커로 같은 파이프라인 재현)

양쪽에 복사해 두면 한쪽만 고쳐졌을 때 **채점 기준과 실제 대화가 조용히 어긋납니다.**
예를 들어 "끊겠습니다" 판정 정규식은 통화 중 상대의 행동(첫 번째는 붙잡고 두 번째에 끊음)과
리포트의 `전화 종료(빠른 판단)` 방어행동 채점을 동시에 결정합니다. 한쪽만 넓히면
대화는 그대로인데 점수만 바뀝니다. 그래서 정의는 한 곳에만 둡니다.

백엔드는 `backend/app/training/scenarios.py`의 `ensure_ai_importable()`로 이 패키지를 찾습니다.
저장소 루트(로컬 실행)와 `/packages`(compose가 `../ai`를 마운트하는 경로)를 순서대로 탐색합니다.

---

## 모듈 지도

| 모듈 | 역할 | 전화 경로 | 로컬 하네스 |
| --- | --- | :---: | :---: |
| `config.py` | 환경변수 로딩과 접근자 | ○ (일부) | ○ |
| `safety.py` | 안전 규칙 프롬프트 + 출력 후처리 | △ | ○ |
| `hangup.py` | 통화 종료 의사 판정 | ○ | ○ |
| `classifier.py` | 행동 라벨 정의 + 분류기 | △ | ○ |
| `voices.py` | 계정에서 실제로 동작하는 보이스 목록 | ○ | ○ |
| `scenarios/` | 시나리오 정의 · 라이브러리 · 동적 생성 | ○ | ○ |
| `sentences.py` | 스트리밍 텍스트를 발화 단위로 절단 | ✕ | ○ |
| `llm_stream.py` | 문장 단위 스트리밍 LLM | ✕ | ○ |
| `stt_stream.py` | Deepgram 스트리밍 STT | ✕ | ○ |
| `tts_stream.py` | ElevenLabs 스트리밍 TTS | ✕ | ○ |
| `audio_io.py` | 마이크 / 스피커 어댑터 | ✕ | ○ |
| `conversation_pipeline.py` | 위 네 개를 이어 붙인 한 턴 | ✕ | ○ |
| `test_latency.py` · `test_stt.py` | 측정 하네스 | ✕ | ○ |
| `harness.py` | 문장 가드 + 통화 모니터 (docs/harness.md) | ○ | ✕ |
| `scenarios/intents.py` · `scenarios/script.py` | 대본 모드 의도 분류·라우터 (docs/script-mode.md) | ○ | ✕ |
| `prerender.py` | 고정 대사 사전 합성·캐시 (`python -m ai.prerender`) | ○ | ✕ |
| `script_eval.py` | 대본 적중률·정확도 측정 (`python -m ai.script_eval`) | ✕ | ✕ |

**△ 표시가 중요합니다.** 전화 경로는 `classifier.py`에서 **라벨 튜플만** 가져다 쓰고
(`RISK_LABELS`, `DEFENSE_LABELS`), 분류 호출은 `report_service.py`가 자체적으로 합니다.
`safety.py`도 `SAFETY_RULES`(프롬프트)는 쓰지만 `sanitize_spoken_text()`(출력 후처리)는
거치지 않습니다 — [알려진 제약](#알려진-제약) 참조.

**✕ 표시 모듈은 전화 통화에 영향을 주지 않습니다.** `sentences.py`의 `_SOFT_FLUSH_LEN`을
고쳐도 전화 경로는 통화 SDK의 문장 분할을 쓰므로 아무것도 바뀌지 않습니다.
로컬에서 재현이 안 될 때 이 표를 먼저 확인하세요.

---

## 1. 시나리오 시스템

### 1.1 Playbook → Scenario

시나리오는 **대본이 아니라 지침**입니다. 대사는 매 턴 실시간 LLM이 새로 쓰고,
Playbook은 그 대사가 놓일 사건·목표·압박 방향을 고정합니다.

```
Playbook (ai/scenarios/library.py, 수기 작성)
  ├ persona_name / organization / role   누가 거는가
  ├ opening_line                          연결 직후 나가는 고정 인사 (LLM 미경유)
  ├ incident                              통화 내내 바뀌면 안 되는 사건
  ├ goal / turn_plan / objection_handling 무엇을 얻으려 하고 어떻게 받아치는가
  ├ examples                              퓨샷 — 길이와 말투를 여기서 가르친다
  ├ quick_replies / hangup_line           LLM 없이 답할 발화
  ├ progression / script                  대본 모드에서 그대로 읽는 대사 (미리 합성)
  └ tactics / red_flags / ideal_...       통화 후 리포트 채점 기준으로 재사용

        ↓ playbook.py : build_system_prompt() + to_scenario()

Scenario.system_prompt = SAFETY_RULES + _STYLE_RULES + 시나리오 블록 + 퓨샷
```

`turn_plan`과 `objection_handling`은 **대사 원문이 아니라 행동 지시**로 씁니다.
대사를 그대로 박아 두면 LLM이 그걸 낭독해서 대화가 죽습니다.

> **프롬프트 배치는 의도적입니다.** `SAFETY_RULES`와 `_STYLE_RULES`는 다섯 시나리오에서
> 바이트 단위로 동일하고 맨 앞에 옵니다(약 733자). 프리픽스 캐시를 쓰는 공급자에서
> 이 구간이 적중하도록 고정 부분을 앞에, 변동 부분을 뒤에 두었습니다.

### 1.2 고정 라이브러리 — 현재 기본값

| `id` | 시나리오 | 유형 | 난이도 | `max_turns` |
| --- | --- | --- | :---: | :---: |
| `bank_security_hold` | 해외 결제 승인 가로채기 | 기관사칭형 | 중 | 8 |
| `low_interest_loan` | 대환대출 선상환 요구 | 대출사기형 | 하 | 7 |
| `delivery_payment_error` | 이중 결제 환불 확인 | 결제사칭형 | 하 | 8 |
| `family_emergency` | 폰 고장 자녀 사칭 | 지인사칭형 | 중 | 8 |
| `investigation_unit` | 명의도용 수사 협조 압박 | 수사기관사칭형 | 상 | 9 |

- `get_scenario(id)` — 모르는 id는 기본 시나리오로 폴백하되 **요청한 id는 유지**합니다.
  `CALL_SCENARIO`가 아직 플레이북이 없는 훈련 유형을 가리켜도 통화가 깨지지 않게 하려는 처리입니다.
- `pick_scenario()` — 직전에 뽑힌 것을 후보에서 빼서 연속된 두 통화가 겹치지 않게 합니다.
  (`_last_picked_id`는 모듈 수준 상태이므로 프로세스 단위입니다.)
- `voice_phishing_training`은 구 id입니다. `_ALIASES`가 `bank_security_hold`로 넘겨
  기존 배포의 `CALL_SCENARIO` 설정이 그대로 동작합니다.

> **통화마다 시나리오를 새로 만들지 않습니다(2026-09-24 결정).** 예전에는 통화 직전에 LLM이
> 시나리오를 쓰는 동적 생성(`generator.py`)이 있었지만 삭제했습니다.
>
> - 대본 모드는 대사가 통화 전에 정해져 있어야 미리 합성·즉시 재생할 수 있습니다.
>   직전에 생성된 시나리오에는 대사도 음성도 없어서 모든 턴이 다시 LLM으로 갑니다.
> - 생성+검수 왕복(최대 6회)이 곧 전화가 울리기 전의 대기였습니다.
> - 사람이 검토하고 테스트가 금지어를 검사한 대사만 통화에 나가야 합니다.
>
> 같은 대본을 두 번 만나는 문제는 **시나리오 편수와 변형 대사를 늘려서** 풉니다.
> LLM은 통화 때가 아니라 새 플레이북 초안을 쓸 때 쓰고, 사람이 다듬어 `library.py`에 넣습니다.

### 1.3 대본 모드와 하네스

- 대본 모드(`CALL_SCRIPT_MODE`): [`../docs/script-mode.md`](../docs/script-mode.md)
- 통화 하네스(문장 가드·통화 모니터): [`../docs/harness.md`](../docs/harness.md)

### 1.4 모델은 교체 가능한 부품

통화 중 응답 모델은 `CALL_LLM_PROVIDER` · `OPENAI_MODEL` / `GEMINI_MODEL`로 지정합니다.
두 키가 모두 있으면 1순위가 첫 토큰 전에 실패할 때 다른 쪽이 그 턴을 대신 답합니다.
리포트 생성은 Gemini의 OpenAI 호환 엔드포인트(`config.GEMINI_OPENAI_BASE_URL`)로
같은 `AsyncOpenAI` 클라이언트를 씁니다.

> **주의: 숨은 추론 토큰이 켜진 모델은 피해야 합니다.** 추론 토큰은 첫 가시 토큰보다
> 먼저 소모되므로 전화에서는 그대로 무음이 됩니다. 측정 결과 비-lite 계열 flash 모델은
> 28–85초가 걸리고 본문이 비어 오는 경우도 있었습니다.

---

## 2. 안전 규칙

AI가 사기범을 연기하되 **실제 범죄에 재사용 가능한 산출물**을 내지 않도록 세 겹으로 막습니다.

| 계층 | 위치 | 차단 대상 |
| --- | --- | --- |
| 프롬프트 규칙 | `safety.SAFETY_RULES` | 실명 기관 사칭, 계좌·카드·주민번호·인증번호 발화, 전화번호·URL·앱 패키지명, AI/훈련임을 밝히는 것 |
| 출력 후처리 | `harness.OutputGuard` (+ `safety.sanitize_spoken_text()`) | 실시간 LLM의 모든 문장: 역할 이탈·실명 기관·비밀정보 요구 차단, 숫자·URL 치환 |
| 대사 검증 | `safety.REAL_ORGS` · `SPOKEN_META` · `UNSAFE_TOKEN` | 시나리오의 모든 필드와 대본 대사 (테스트에서 검사) |

기관명은 전부 가상입니다. 훈련자가 실명 기관을 먼저 언급하더라도 그 기관 직원을 사칭하지
않습니다. 실재 기관명은 **해당 기관과 협력하는 형태로만** 도입할 수 있는 항목으로 분류해
두었습니다 — 훈련 몰입도에는 도움이 되지만 협력 없이 넣을 성질의 것이 아닙니다.

이 규칙들은 `backend/tests/test_scenario_library.py`가 실제로 검사합니다.
시나리오를 추가하면 그 테스트가 새 시나리오도 자동으로 검사합니다.

---

## 3. 통화 중 판정 로직

### 3.1 종료 의사 — `hangup.py`

```python
wants_hang_up("이만 삼천 원 결제됐다고요?")      # False — 금액의 "이만"
wants_hang_up("네 알겠습니다, 이만 끊을게요")     # True
wants_hang_up("그 사람이 전화 끊으라던데요, 무슨 일이죠?")  # False — 발화 앞부분의 인용
```

세 가지 도메인 판단이 들어 있습니다.

- **거절은 종료가 아닙니다.** "안 할래요", "됐어요"는 요청에 대한 거부이지 통화를 끝내겠다는
  뜻이 아니고, 시나리오는 그 구분에 의존해 계속 압박합니다. 그래서 패턴에 넣지 않았습니다.
- **"이만"은 뒤에 종료 동사가 올 때만** 셉니다. 단독으로는 숫자 20,000이고,
  `delivery_payment_error`의 피해 금액이 실제로 "이만 삼천 원"입니다.
- **발화 끝 30자(`HANG_UP_TAIL_CHARS`) 안에서만** 인정합니다. 통화를 끝내겠다는 선언은
  마지막에 옵니다. 긴 답변 앞부분의 같은 표현은 대개 인용입니다.

마지막 항목은 파이프라인이 발화 조각을 하나로 병합하게 되면서 더 중요해졌습니다.
이 함수가 보는 텍스트가 이제 문장 조각이 아니라 답변 전체이기 때문입니다.

### 3.2 즉답 테이블 — `scenarios/reflex.py`

답이 시나리오에서 이미 정해진 발화는 LLM 왕복을 통째로 건너뜁니다.

| 트리거 | 예시 발화 |
| --- | --- |
| `scam_accusation` | "이거 보이스피싱 아니에요?" |
| `not_audible` | "안 들려요", "소리가 작아요" |
| `repeat_that` | "뭐라고요?", "다시 말해 주세요" |
| `who_is_this` | "누구세요?", "어디시죠?" |
| `busy_now` | "지금 바빠요", "운전 중이에요" |

순서가 의미를 갖습니다. 먼저 매칭되는 패턴이 이기므로 구체적인 의도(사기 지적)를
일반적인 것("누구세요") 앞에 둡니다. `scam_accusation`은 **지연 개선과 실감이 같은 방향**입니다 —
실제 사기범은 그 질문에 망설이지 않습니다.

세 가지 제약을 둡니다.

- 트리거당 한 통화에 **한 번만** — 같은 말을 두 번 하지 않도록
- 통화 전체 `CALL_REFLEX_BUDGET`회(기본 3, `0`이면 비활성)
- `MAX_REFLEX_CHARS`=40자 초과 발화는 매칭에서 제외 — 긴 문장 안의 같은 단어는 전혀
  다른 의도이고, `search()`로는 구분할 수 없습니다. 문단을 "가온금융안전원 서동현입니다."로
  받는 것은 한 박자 늦게 답하는 것보다 나쁩니다.

### 3.3 행동 라벨 — `classifier.py`

**점수는 여기서 계산하지 않습니다.** 이 모듈은 근거와 함께 행동을 라벨링만 하고,
점수 산식은 백엔드 `report_service.calculate_response_score()`가 갖습니다.
같은 대화를 두 번 채점해도 같은 점수가 나오게 하려는 분리입니다.

| 위험행동 (7) | 방어행동 (6) |
| --- | --- |
| 개인정보 제공 · 금융정보 제공 · 상대방 기관명 신뢰 · 송금 의사 표현 · 링크 접근 의사 · 앱 설치 의사 · 통화 장시간 지속 | 상대방 신원 확인 · 공식 대표번호 확인 의사 · 개인정보 제공 거절 · 송금 거절 · 전화 종료(빠른 판단) · 신고 의사 표현 |

`_filter_known_labels()`가 목록에 없는 라벨과 근거가 빈 항목을 버립니다.
백엔드는 여기서 **라벨 튜플만** import하고 분류 호출은 자체 프롬프트로 합니다.

### 3.4 보이스 — `voices.py`

```python
WORKING_VOICE_IDS   = (THEO, YOHAN_KOO, Kelee_K, Onyu)   # 이 계정에서 합성 확인됨
AVAILABLE_VOICE_IDS = (THEO, YOHAN_KOO, Kelee_K)          # random_voice_id()의 후보
```

`Hanna`와 `Zara`는 **이 계정에서 아무 소리도 나지 않습니다.** Voice Library 보이스는
유료 플랜이 필요하고, API가 402를 반환하며 스트림이 0바이트로 닫힙니다.
통화에서 그것은 음질 저하가 아니라 **완전한 무음**입니다.
시나리오에 보이스를 배정할 때는 반드시 위 목록 안에서 고르세요.

---

## 4. 로컬 음성 파이프라인

전화 없이 마이크·스피커로 같은 흐름을 재현합니다. 각 단계가 제너레이터라
어댑터만 갈아 끼우면 전화망으로 옮겨갈 수 있는 구조입니다.

```
AudioSource → stt_stream → llm_stream → tts_stream → AudioSink
(LocalMicSource)                                     (LocalSpeakerSink / NullAudioSink)
```

`conversation_pipeline.run_turn()`이 한 턴을 담당하고, `LatencyMetrics`에
네 개의 시점을 찍습니다: LLM 첫 토큰 / 첫 문장 완성 / TTS 첫 바이트 / 종단.

### 문장 절단 — `sentences.py`

전체 응답을 버퍼링하지 않습니다. **말할 수 있는 문장이 완성되는 즉시** 흘려보내
TTS가 바로 시작하게 합니다.

- `.!?。！？\n`을 만나면 즉시 절단 (길이 무관)
- `3.14` 같은 소수점은 문장 끝으로 보지 않음
- 문장부호 없이 `_SOFT_FLUSH_LEN`(88자)을 넘으면 마지막 쉼표에서 절단
- `_HARD_FLUSH_LEN`(128자)을 넘으면 강제 절단

88/128은 64/96에서 올린 값입니다. 한 덩어리를 더 모아 자연스럽게 읽히게 하는 대신
지연을 조금 내준 것이므로, 지연이 우선이면 다시 내리면 됩니다.
절단할 때마다 `sanitize_spoken_text()`를 통과시킵니다.

---

## 5. 실행 방법

```bash
# 저장소 루트에서
pip install -e .                    # pyproject.toml — test-stt, test-latency 스크립트 등록

python -m ai.test_stt               # 마이크 → Deepgram 스트리밍 확인 (음성 출력 없음)
python -m ai.test_latency           # 타이핑 입력 모드
python -m ai.test_latency --mic     # 마이크 입력 — 발화 종료 → 첫 TTS 바이트 측정
python -m ai.prerender --dry-run    # 미리 합성할 대사 목록 (--dry-run 빼면 실제 합성)
python -m ai.script_eval --logs app.log   # 대본 모드 적중률
```

`python -m ai`는 `test_latency`로 연결됩니다(`__main__.py`).

**목표: 발화 종료 → 첫 오디오 바이트 1,000ms 미만.**

측정 하네스는 실행 시 어떤 공급자·모델을 재는지 첫 줄에 출력합니다.
이전 버전은 `AsyncOpenAI`를 직접 만들어 `CALL_LLM_PROVIDER=gemini`인데도 **항상 OpenAI를
측정**했습니다. 즉 프로덕션 경로를 한 번도 재지 못했습니다. 지금은 `config.call_llm_provider()`를
따라갑니다.

---

## 6. 환경변수

`config.py`는 `ai/.env`를 먼저 읽고, 없는 값만 `backend/.env`에서 채웁니다(`override=False`).

> **`ai/.env`를 새로 만들지 마세요.** 2026-09-05에 `backend/.env` 단일 파일로 통합했습니다.
> `ai/.env`가 있으면 `config.py`가 그걸 먼저 읽어 진입점마다 값이 갈립니다.
> 실제로 그 때문에 `ELEVENLABS_MODEL_ID`가 코드 기본값과 다르게 잡혀, 전화 TTS가
> 저지연 모델이 아니라 품질 우선 모델로 돌고 있었던 적이 있습니다.

| 그룹 | 변수 | 기본값 |
| --- | --- | --- |
| 공급자 | `CALL_LLM_PROVIDER` | `openai` |
| | `OPENAI_MODEL` / `GEMINI_MODEL` | `gpt-4o-mini` / `gemini-3.5-flash-lite` |
| STT | `DEEPGRAM_MODEL` · `DEEPGRAM_LANGUAGE` | `nova-2` · `ko` |
| | `STT_SAMPLE_RATE` | `16000` |
| | `STT_ENDPOINTING_MS` | `300` — **체감 지연의 실질적 하한** |
| | `STT_UTTERANCE_END_MS` | `1000` — `speech_final`이 안 올 때의 백스톱 |
| TTS | `ELEVENLABS_MODEL_ID` | `eleven_flash_v2_5` |
| | `ELEVENLABS_VOICE_ID` | 비우면 시나리오별 배정 사용 |
| | `ELEVENLABS_OUTPUT_FORMAT` · `_SAMPLE_RATE` | `pcm_24000` · `24000` |
| 시나리오 | `CALL_SCENARIO` | 비우면 매 통화 무작위 선택 |
| 대본 모드 | `CALL_SCRIPT_MODE` | `shadow` (`off` / `shadow` / `on`) |
| | `CALL_PRERENDER` · `TTS_CACHE_DIR` · `ELEVENLABS_PRERENDER_MODEL_ID` | `true` · 임시 폴더 · 실시간 모델과 같게 |
| 하네스 | `CALL_GUARD_MAX_VIOLATIONS` · `CALL_MAX_SENTENCES` | `3` · `3` |

`STT_ENDPOINTING_MS`가 진짜 손잡이입니다. 정상 흐름에서 발화는 `speech_final`로 마감되므로
400 → 300(현재 기본값)은 매 턴 약 100ms, 250까지 내리면 150ms가 산술적으로 그대로 줄어듭니다.
다만 말이 느린 사람의 문장 중간을 끊을 수 있으니 **실제 훈련자 연령대로 시험한 뒤** 정하세요.

---

## 7. 새 시나리오 추가하기

1. `library.py`에 `Playbook`을 한 편 추가하고 `PLAYBOOKS` 튜플에 넣습니다.
2. `incident`에 시각·대상·금액을 **한글 말로** 못 박습니다. 통화 내내 바뀌면 안 됩니다.
3. `turn_plan`·`objection_handling`은 대사가 아니라 **행동 지시**로 씁니다.
4. `examples`에 퓨샷을 넣습니다. 답변은 전부 "짧은 문장 두 개" 길이로 씁니다 —
   길이는 지시문보다 예시가 훨씬 잘 가르칩니다.
5. `quick_replies`의 답은 **대화 어느 시점에 나와도 어색하지 않아야** 합니다.
6. `tts_voice_id`는 `voices.WORKING_VOICE_IDS` 안에서 고릅니다.
7. `progression`(동의할 때마다 다음 줄)과 `script`(의도별 답변 2개씩)를 씁니다. **그대로 읽히는 대사**이므로 한 줄에 짧은 문장 한두 개만.
8. `tactics` / `red_flags` / `ideal_trainee_response`는 리포트 채점 기준으로 재사용되므로,
   `red_flags`에는 `turn_plan`에서 **실제로 드러나는** 위험만 적습니다.
9. `pytest backend/tests/test_scenario_library.py backend/tests/test_script_mode.py` — 안전 정규식 검사는 자동으로 확장됩니다.
10. `python -m ai.prerender --scenario <id>`로 대사를 미리 합성해 둡니다.

세트 전체로는 사건 유형·기관 유형·말투·압박 방식이 겹치지 않게 하고 난이도를 고르게 둡니다.
자세한 기준은 [`scenarios/scenario_generation_guidelines.md`](scenarios/scenario_generation_guidelines.md)에 있습니다.

---

## 알려진 제약

| 항목 | 내용 |
| --- | --- |
| **Realtime 폴백 경로** | Deepgram/ElevenLabs 키가 없어 음성-음성 모델로 떨어지면 텍스트를 가로챌 지점이 없어 하네스 2·3층이 적용되지 않습니다(프롬프트만). |
| **`pick_scenario` 상태** | 중복 방지는 프로세스 메모리(`_last_picked_id`)에 있습니다. 워커가 여러 개면 각자 따로 셉니다. |
| **설정 드리프트** | `.env.example`의 `ELEVENLABS_MODEL_ID`·`GEMINI_MODEL` 값이 코드 기본값 및 측정 결론과 어긋나 있습니다. 무음 통화를 유발할 수 있어 우선 정리 대상입니다. |

## 관련 문서

- [`scenarios/scenario_generation_guidelines.md`](scenarios/scenario_generation_guidelines.md) — 시나리오 작성 규칙
- [`../docs/latency.md`](../docs/latency.md) — 지연 측정 결과와 개선 이력
- `../backend/app/training/` — 이 패키지를 전화 경로에 연결하는 어댑터
