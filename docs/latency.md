# 통화 응답 지연 줄이기

훈련 통화에서 사용자가 체감하는 지연은 두 군데에 있습니다.

1. **전화가 울리기까지** — 발신 버튼을 누르고 실제로 통화가 시작될 때까지
2. **한 턴의 응답 지연** — 훈련자가 말을 마치고 상담원 목소리가 나오기까지

## 1. 전화가 울리기까지

예전에는 통화를 걸기 전에 LLM으로 시나리오를 새로 썼습니다.

`ai/scenarios/generator.py`의 `generate_scenario`는 한 번 시도할 때마다
**생성 호출 1회 + 검수 호출 1회**를 하고, 검수 점수가 팔십 점 미만이면
실패 사유를 되먹여 최대 세 번까지 다시 만듭니다. 즉 **LLM 왕복 2~6회**입니다.
`call_service.py`는 여기에 `SCENARIO_GENERATION_TIMEOUT_SEC`(기본 20초)의
상한을 걸어 두었고, 타임아웃이 나면 그 20초를 다 쓰고 나서야 폴백했습니다.

지금은 `ai/scenarios/library.py`의 고정 시나리오 다섯 편 중 하나를
프로세스 안에서 고릅니다. **LLM 왕복 0회.**

`DYNAMIC_SCENARIO=true`로 두면 예전 동작으로 돌아갑니다.

## 2. 한 턴의 응답 지연

한 턴은 이렇게 흘러갑니다.

```
훈련자 말 끝
  → Deepgram이 발화 끝을 확정        (침묵 감지 대기)
  → LLM 첫 토큰                       (요청 왕복 + 모델 시작)
  → 첫 문장 완성                      (토큰 생성)
  → ElevenLabs 첫 오디오 바이트       (TTS 시작)
  → 8kHz ulaw 변환 후 전화망 송출
```

가장 크게 손해 보는 곳은 앞의 세 단계입니다. 마지막 변환은 무시할 수준입니다.

### 이번에 적용한 것

| 무엇 | 어디 | 효과 |
| --- | --- | --- |
| 시나리오 사전 생성 제거 | `ai/scenarios/library.py` | 통화 시작 전 LLM 왕복 2~6회 → 0회 |
| ElevenLabs 모델 교체 | `call_service.py` `build_pipeline_session` | 아래 설명 참고. 이번 변경 중 턴당 체감이 가장 큽니다 |
| 즉답 경로 | `pipeline_session.py` `_handle_final_transcript` | 해당 턴은 LLM 왕복이 통째로 사라집니다 |
| Gemini thinking budget 0 | `call_service.py` `_gemini_thinking_kwargs` | 숨은 reasoning 토큰 제거 |
| `max_tokens` 180 → 120 | `call_service.py` `_call_max_tokens` | 첫 토큰은 그대로, 긴 답변 꼬리만 자릅니다 |
| `utterance_end_ms` 1200 → 1000 | `call_service.py` `build_pipeline_session` | 아래 설명 참고 |
| 프롬프트 중복 제거 | `call_service.py` `phone_system_prompt` | 입력 토큰 감소 |

#### ElevenLabs 모델이 잘못 잡혀 있었습니다

`backend/app/main.py`는 `backend/.env`를 먼저 읽고, 그다음 `ai/.env`를
`override=False`로 읽습니다. `backend/.env`에는 `ELEVENLABS_MODEL_ID`가
없었고 `ai/.env`에는 `eleven_multilingual_v2`가 있었습니다.
그래서 코드의 기본값(`eleven_turbo_v2_5`)은 한 번도 적용된 적이 없고,
전화 통화 TTS가 ElevenLabs의 **품질 우선 모델**로 돌고 있었습니다.

지금은 두 `.env`와 코드 기본값을 모두 `eleven_flash_v2_5`로 맞췄습니다.
ElevenLabs가 공개한 flash v2.5의 모델 지연은 약 75ms입니다.
multilingual v2와의 실제 차이는 회선에서 직접 재 보십시오.

음질이 아쉬우면 `ELEVENLABS_MODEL_ID=eleven_turbo_v2_5`가 중간 선택지입니다.

#### 즉답 경로 (reflex)

"안 들려요", "누구세요?", "지금 바빠요", "이거 보이스피싱 아니에요?" 처럼
답이 시나리오에서 이미 정해져 있는 발화는 LLM을 부르지 않고
`ai/scenarios/reflex.py`의 고정 문장으로 바로 답합니다.

- 트리거당 한 통화에 한 번만 (같은 말을 두 번 하지 않게)
- 한 통화 전체로 `CALL_REFLEX_BUDGET`번까지 (기본 3, `0`이면 끔)
- 나머지 턴은 전부 LLM이 씁니다

특히 "이거 보이스피싱 아니에요?"는 **즉답이 실감에도 유리합니다.**
진짜 사기범은 이 질문에 망설이지 않습니다.

#### utterance_end_ms는 백스톱입니다

`PhoneDeepgramSTT`는 `endpointing`(기본 400ms)과 `utterance_end_ms`를 함께 보냅니다.
`UtteranceAssembler`는 `speech_final`과 `UtteranceEnd` 중 **먼저 오는 쪽**으로
발화를 마감합니다. 정상 흐름에서는 `speech_final`(400ms)이 이기므로,
`utterance_end_ms`는 Deepgram이 `speech_final`을 안 보낼 때만 걸립니다.
`deepgram_stt.py`의 모듈 설명이 그런 경우가 실제로 있다고 적어 두었으니,
1200 → 1000은 그 폴백 경로에서만 200ms를 아낍니다.

더 줄이려면 `STT_ENDPOINTING_MS`를 400에서 250~300으로 내리는 쪽이
효과가 큽니다. 다만 말이 느린 사람의 문장 중간을 끊을 수 있으니
실제 훈련자 연령대로 시험해 보고 정하십시오.

## 3. 프롬프트만으로 줄이는 법

프롬프트 길이는 생각만큼 큰 변수가 아닙니다. 지금 시스템 프롬프트는
한국어 2,100자 정도이고, flash-lite급 모델이 이 정도 입력을 읽는 데 드는
시간은 수십 ms 수준입니다. **프롬프트로 얻는 진짜 이득은 출력 쪽입니다.**

효과가 큰 순서대로:

1. **한 응답의 길이를 못 박는다.** "짧은 문장 두 개까지"를 규칙으로 두고,
   퓨샷 예시의 답변도 전부 그 길이로 씁니다. 지시문보다 예시가 훨씬 잘 먹습니다.
   `ai/scenarios/playbook.py`의 `[말의 길이와 결은 이 정도로 한다]` 블록이 그 역할입니다.
2. **첫 문장을 특히 짧게 시작하라고 지시한다.** TTS가 문장 단위로 스트리밍되므로
   첫 문장이 짧으면 첫 오디오 바이트가 그만큼 빨리 나갑니다.
3. **서두와 마크다운을 금지한다.** "대사만 말한다. 목록, 마크다운, 괄호 지문,
   상황 설명을 쓰지 않는다." 모델이 `**` 나 `1.` 을 뱉으면 TTS가 그걸 읽거나
   문장 분할이 어긋납니다.
4. **부정문을 줄이고 지시문으로 바꾼다.** "~하지 마라"를 길게 나열하면
   모델이 규칙 검토에 출력을 쓰고 답이 길어집니다. 안전 규칙처럼 꼭 필요한
   금지만 남기고 나머지는 "~한다"로 씁니다.
5. **고정 부분을 앞에, 변동 부분을 뒤에 둔다.** 다섯 시나리오의 시스템 프롬프트는
   앞 733자가 완전히 동일합니다(안전 규칙 + 말하는 방식). 프리픽스 캐시를 쓰는
   제공자라면 이 부분이 적중합니다. 다만 캐시 최소 토큰 요건을 넘지 못하면
   적중하지 않으니, 이건 공짜로 얻는 보험 정도로 보십시오.
6. **히스토리를 잘라 낸다.** 최대 턴이 7~9이라 지금은 문제가 되지 않지만,
   턴을 늘리면 최근 여섯 왕복만 남기는 슬라이딩 윈도우를 넣을 만합니다.

## 4. 더 줄이고 싶을 때 (아직 적용 안 함)

| 방법 | 예상 절감 | 난이도 | 위험 |
| --- | --- | --- | --- |
| 첫 인사말 오디오 사전 합성 | 통화 시작 시 TTS 왕복 1회 | 중 | `ELEVENLABS_VOICE_RANDOM=true`면 목소리가 매번 달라 캐시가 안 먹습니다. 끄고 써야 합니다 |
| 필러 발화("네,")를 먼저 흘리기 | 체감만 개선 | 하 | 매 턴 같은 필러면 금방 티가 납니다 |
| interim 전사로 응답 투기적 생성 | LLM 왕복의 상당 부분 | 상 | 훈련자가 말을 이어 가면 버린 생성이 낭비되고, 잘못 이어 붙이면 대화가 깨집니다 |
| 서버를 한국/일본 리전으로 | 왕복당 수십~수백 ms | 중 | 없음 |
| LLM·TTS 커넥션 사전 워밍업 | 첫 턴의 TLS 핸드셰이크 | 중 | 없음 |
| Realtime/Live API (음성 to 음성) | 파이프라인 전체 | 상 | STT/TTS 제어권과 문장 단위 안전 필터를 잃습니다 |

동시 통화도 확인해 보십시오. `call_service.py`의 `_outbound_lock`이 발신을
직렬화해서, 두 번째 통화는 최대 20초를 기다린 뒤 실패합니다.
지연이 아니라 처리량 문제지만 같이 걸립니다.

## 5. 재는 법

```
python -m ai.test_latency --mic
```

`발화 종료 → 첫 TTS 오디오 바이트`를 찍어 줍니다. 목표는 1000ms 미만입니다.

한 가지 주의: `ai/test_latency.py`는 LLM 클라이언트를
`AsyncOpenAI(api_key=openai_api_key())`로 직접 만들기 때문에
`CALL_LLM_PROVIDER=gemini`여도 **로컬 측정은 항상 OpenAI를 씁니다.**
전화 경로의 Gemini 지연을 재려면 실제 통화 로그를 봐야 합니다.

또 하나: `ai/sentences.py`(와 그 안의 `sanitize_spoken_text`)는
로컬 `ai/` 파이프라인에서만 쓰입니다. 전화 경로는 ClawOps의 문장 분할을
쓰므로 `ai/sentences.py`의 `_SOFT_FLUSH_LEN` 같은 값을 고쳐도
전화 통화에는 영향이 없습니다. 같은 이유로 전화 경로의 발화는
`sanitize_spoken_text`를 거치지 않습니다. 안전은 프롬프트 규칙에만 기대고 있습니다.
