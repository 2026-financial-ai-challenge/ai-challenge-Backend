import os
import sys
from pathlib import Path


def ensure_ai_importable() -> Path:
    """저장소 루트의 ai 패키지를 백엔드에서 import할 수 있게 경로를 추가한다."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3],  # 저장소에서 실행할 때
        Path("/packages"),  # Docker 이미지에 ai를 복사한 위치
    ]
    for root in candidates:
        if (root / "ai" / "scenarios" / "__init__.py").is_file():
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
            return root

    raise RuntimeError(
        "ai package not found. Run the API from the repo, or mount ../ai at /packages/ai."
    )


def get_call_scenario():
    ensure_ai_importable()
    from ai.scenarios import get_scenario

    return get_scenario(os.getenv("CALL_SCENARIO", "voice_phishing_training"))


def get_runtime_scenario(training_type: str | None = None):
    """통화 한 건의 시나리오를 고른다.

    기본은 고정 시나리오 중 무작위. CALL_SCENARIO는 모든 통화를,
    ANNOUNCED_CALL_SCENARIO는 1차(예고) 통화만 해당 시나리오로 고정한다.
    """
    ensure_ai_importable()
    from ai.scenarios import get_scenario, pick_scenario

    announced_pin = os.getenv("ANNOUNCED_CALL_SCENARIO", "").strip()
    if training_type == "announced" and announced_pin:
        return get_scenario(announced_pin)
    if os.getenv("CALL_SCENARIO", "").strip():
        return get_call_scenario()
    return pick_scenario()
