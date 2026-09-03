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
    """Return a per-call generated scenario when dynamic mode is enabled."""
    base = get_call_scenario()
    from ai.scenarios.generator import dynamic_scenarios_enabled, generate_scenario

    if not dynamic_scenarios_enabled():
        return base
    return await generate_scenario(base)
