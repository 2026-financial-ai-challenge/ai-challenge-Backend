"""시나리오 문장 회귀 테스트."""

import dataclasses
import re

from ai.scenarios.library import PLAYBOOKS

# "오늘 낮 열두 시까지"처럼 고정 시각을 쓰면 그 시각이 지난 뒤 걸린 통화에서
# 모델이 "아직 안 지났다"고 우기게 된다(2026-09-30 테스트 통화).
# 지난 일인 "어제 오후 두 시"는 언제 걸어도 맞으므로 허용한다.
_CLOCK_TIME = re.compile(r"(?<!어제 )(오전|오후|낮|정오|저녁|밤|아침) [가-힣]+ 시")


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _strings(item)
    elif dataclasses.is_dataclass(value):
        for field in dataclasses.fields(value):
            yield from _strings(getattr(value, field.name))


def test_playbooks_use_relative_times_only():
    hits = [
        (playbook.id, text)
        for playbook in PLAYBOOKS
        for text in _strings(playbook)
        if _CLOCK_TIME.search(text)
    ]
    assert hits == []
