"""끊겠다는 말 판정 회귀 테스트. 리포트의 "전화 종료(빠른 판단)" 점수가 이 판정을 쓴다."""

import pytest

from ai.hangup import wants_hang_up


@pytest.mark.parametrize("text", ["끊을게요.", "안 믿어요. 끊겠습니다", "이만 끊을게요", "수고하세요"])
def test_closing_words_count(text):
    assert wants_hang_up(text)


@pytest.mark.parametrize("text", [
    "",
    "됐어요.",  # 거절은 끊겠다는 말이 아니다
    "이만 삼천 원이요?",  # 숫자 '이만'
    "그 사람이 전화 끊으라고 하던데요, 그래서 지금 은행에 직접 확인해 보려고 기다리는 중이에요",  # 남의 말을 옮김
])
def test_other_words_do_not_count(text):
    assert not wants_hang_up(text)
