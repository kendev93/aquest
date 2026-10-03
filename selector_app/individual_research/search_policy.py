"""Shared, versioned research search policy (not a trading configuration)."""

from typing import Final, Literal

SEARCH_POLICY_ID: Final[Literal["adaptive-v1"]] = "adaptive-v1"
CANDIDATE_LIMIT = 15
MEDIUM_LIMIT = 120
FULL_LIMIT = 40
CONVERGENCE_MIN_TRIALS = 1500
CONVERGENCE_PATIENCE = 1000
IMPROVEMENT_RELATIVE = 0.01
IMPROVEMENT_ABSOLUTE = 1e-6


def search_policy_settings() -> dict[str, object]:
    return {
        "id": SEARCH_POLICY_ID,
        "candidate_limit": CANDIDATE_LIMIT,
        "medium_limit": MEDIUM_LIMIT,
        "full_limit": FULL_LIMIT,
        "convergence_min_trials": CONVERGENCE_MIN_TRIALS,
        "convergence_patience": CONVERGENCE_PATIENCE,
        "improvement_relative": IMPROVEMENT_RELATIVE,
        "improvement_absolute": IMPROVEMENT_ABSOLUTE,
        "trigger_correlation": 0.9,
    }
