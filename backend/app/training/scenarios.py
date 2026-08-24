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
    from ai.scenarios import SCENARIOS, get_scenario

    scenario_id = os.getenv("CALL_SCENARIO", "institution_impersonation").strip()
    if not scenario_id:
        scenario_id = "institution_impersonation"
    try:
        return get_scenario(scenario_id)
    except KeyError as exc:
        known = ", ".join(SCENARIOS)
        raise RuntimeError(
            f"Unknown CALL_SCENARIO '{scenario_id}'. Expected one of: {known}"
        ) from exc
