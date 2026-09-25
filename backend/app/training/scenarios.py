import os
import sys
from pathlib import Path


def ensure_ai_importable() -> Path:
    """Make the repo-root `ai` package importable from the backend process."""
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3],  # repo root when running from backend/app/...
        Path("/packages"),  # docker compose mount of ../ai
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


async def get_runtime_scenario():
    """Pick the scenario for one outbound training call.

    Always a fixed playbook from ai/scenarios/library.py, chosen at random so
    consecutive calls differ, with no LLM round trip. Pinning CALL_SCENARIO to
    one id disables the rotation.

    There is deliberately no per-call generation: the script mode speaks
    pre-written lines that are synthesized before the call, and a scenario
    written seconds before dialing has neither the lines nor the audio.
    """
    ensure_ai_importable()
    pinned = os.getenv("CALL_SCENARIO", "").strip()
    if pinned:
        return get_call_scenario()

    from ai.scenarios import pick_scenario

    return pick_scenario()
