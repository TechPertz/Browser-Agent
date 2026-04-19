"""Specialist planners — task-type-specialized prompts over the same runtime.

Each specialist is a (system_prompt, action_hints) pair. The planner
node picks one based on task_type classification. Same LangGraph
executes all of them; specialists differ only in the system prompt +
plan template, not in the action vocabulary.

Keeping specialists as prompts rather than separate sub-graphs buys
us "many subagents" per the spec while avoiding the complexity cost of
distinct graph topologies. If specific task types later need a
different execution shape, promote them to sub-graphs then.
"""

from .prompts import (
    EXTRACT_SPECIALIST_SYSTEM,
    FORM_FILL_SPECIALIST_SYSTEM,
    GENERIC_SPECIALIST_SYSTEM,
    LIST_ITER_SPECIALIST_SYSTEM,
    NAVIGATE_SPECIALIST_SYSTEM,
    system_prompt_for,
)

__all__ = [
    "EXTRACT_SPECIALIST_SYSTEM",
    "FORM_FILL_SPECIALIST_SYSTEM",
    "GENERIC_SPECIALIST_SYSTEM",
    "LIST_ITER_SPECIALIST_SYSTEM",
    "NAVIGATE_SPECIALIST_SYSTEM",
    "system_prompt_for",
]
