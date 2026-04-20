from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """Which role in the agent pipeline is making this call.

    Role -> model mapping is defined in `config/profile.yaml`. Code asks
    for a Role; the registry resolves the concrete model from the
    profile. This is what makes the trial-day key swap trivial.
    """

    PLANNER = "planner"
    NAVIGATOR = "navigator"
    EXTRACTOR = "extractor"
    JUDGE = "judge"
    # Multimodal role for Set-of-Mark visual resolution. Picks a mark_id
    # and emits a structural descriptor that survives across input rows.
    VISION = "vision"
