"""시나리오 문장 회귀 테스트."""

import dataclasses
import re

from ai.scenarios.library import PLAYBOOKS

# 고정 시각("오늘 낮 열두 시까지")은 그 시각이 지난 뒤 걸린 통화에서 틀린 말이 된다.
# 과거 시각("어제 오후 두 시")은 허용.
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
