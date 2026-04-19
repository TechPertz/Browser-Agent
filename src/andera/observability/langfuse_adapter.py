"""Langfuse integration — opt-in, via env vars.

LiteLLM has a built-in Langfuse callback. All we have to do is:
  - set the Langfuse env vars it expects
  - append "langfuse" to litellm.success_callback (and failure_callback)

When the profile disables it, this module is a no-op. When enabled but
the Langfuse SDK isn't installed, we log a warning and continue.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from andera.config import Profile

log = logging.getLogger(__name__)


def install_langfuse_if_enabled(profile: Profile) -> bool:
    """Return True if Langfuse callback was wired in."""
    lf = profile.observability.langfuse
    if not lf.enabled:
        return False

    public_key = os.environ.get(lf.public_key_env)
    secret_key = os.environ.get(lf.secret_key_env)
    if not public_key or not secret_key:
        log.warning(
            "langfuse enabled in profile but %s / %s missing from env; skipping",
            lf.public_key_env, lf.secret_key_env,
        )
        return False

    try:
        import litellm  # noqa: F401  # required for the callback to attach
    except ImportError:
        log.warning("litellm not installed; cannot enable langfuse callback")
        return False

    try:
        # Langfuse SDK must be present for the callback to do anything.
        import langfuse  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        log.warning("langfuse SDK not installed; skipping (pip install langfuse)")
        return False

    os.environ.setdefault("LANGFUSE_HOST", lf.host)
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", secret_key)

    import litellm as _ll
    _append(_ll, "success_callback", "langfuse")
    _append(_ll, "failure_callback", "langfuse")
    log.info("langfuse callback registered; host=%s", lf.host)
    return True


def _append(module: Any, attr: str, value: str) -> None:
    current = getattr(module, attr, None) or []
    if value not in current:
        current.append(value)
    setattr(module, attr, current)
