[PR_설명_agent-list.md](https://github.com/user-attachments/files/32697326/PR_._agent-list.md)
# feat(ai): 에이전트 목록 명령 추가, 시나리오 id 고정 테스트

**브랜치:** `ai/agent-list-scenario-ids` → `develop`
**변경 범위:** `ai/` 폴더만 (백엔드 코드 변경 없음)

## 요약

1. `python -m ai.managed_agent list` 명령을 추가했어요. ClawOps 계정의 에이전트를 ID와 함께 보여 주고, 문제가 있는 에이전트를 표시해요.
2. 시나리오 id가 바뀌지 않도록 막는 테스트를 추가했어요. 요청 4번에 대한 답이에요.

## 1. `list` 명령

```bash
PYTHONPATH=. python -m ai.managed_agent list --variant external_tts
```

출력 예시:
```
cmagent123  spc-bank_security_hold-external_tts  [external_tts]
cmagent456  가상의 보이스피싱범 2  [external_tts]  [안전 규칙 없음]
(없음) spc-family_emergency-external_tts  -> sync 필요
```

- **각 줄 맨 앞이 Agent ID예요.** 콘솔에서 ID를 찾기 어려워서 만들었어요.
- **`[안전 규칙 없음]`**: 지시문에 `[교육용 시뮬레이션 안전 규칙]` 블록이 없는 에이전트예요. 콘솔에서 손으로 만든 에이전트를 찾아 정리할 때 써요.
- **`(없음) ... -> sync 필요`**: 백엔드가 이름으로 찾는 `spc-<시나리오>-<방식>` 에이전트 중 아직 없는 것이에요. 이게 있으면 그 시나리오의 통화는 `LookupError`가 나고 옛 방식으로 넘어가요.
- `--variant`를 빼면 `live` 5개도 "sync 필요"로 나와요. 지금은 `external_tts`만 쓰니 붙여서 실행하는 게 보기 편해요.

sync 전에 계정 상태를 확인하고, sync 후에 5개가 생겼는지 확인하는 용도예요. 읽기만 하고 아무것도 바꾸지 않아요.

## 2. 시나리오 id 고정 테스트 (`ai/tests/test_scenario_ids.py`)

시나리오 id는 **이름표가 아니라 DB에 저장되는 식별자**예요. `calls.scenario_id`, `CALL_SCENARIO`, 에이전트 이름 `spc-<id>-<variant>`가 모두 이 문자열을 들고 있어요.

누가 id를 바꾸면 `get_scenario()`가 오류 없이 **조용히 기본 시나리오로 넘어가서**, 과거 통화가 엉뚱한 시나리오 기준으로 채점돼요. 비유하면 **사물함 번호를 바꿨더니 예전 열쇠로 남의 사물함이 열리는** 상황이에요.

테스트가 고정하는 것:
| 항목 | 값 |
|---|---|
| 시나리오 id 5개 | `bank_security_hold`, `low_interest_loan`, `delivery_payment_error`, `family_emergency`, `investigation_unit` |
| 기본 시나리오 | `bank_security_hold` |
| 옛 id 별칭 (`_ALIASES`) | `voice_phishing_training` → `bank_security_hold` |
| 에이전트 이름 형식 | `spc-family_emergency-external_tts` |

**id를 꼭 바꿔야 한다면:** 새 id로 바꾸고, **옛 id를 `_ALIASES`에 남긴 뒤** 테스트를 함께 고치면 돼요. 그러면 과거 통화도 올바른 시나리오로 채점돼요.

## 테스트

```bash
python -m pytest ai/tests -q
# 28 passed
```

- 추가한 테스트: `test_scenario_ids.py`(4개), `test_agent_listing_flags_unsafe_and_missing_agents`(1개)
- 실제 ClawOps 계정으로 `list`를 실행해 보지는 않았어요. 로컬에 키가 아직 없어서, 가짜 데이터로 테스트만 했어요.

## 백엔드 영향

없어요. 백엔드가 쓰는 `pick_variant()`, `resolve_agent_id()`, `build_call_context()`는 바뀌지 않았어요.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
