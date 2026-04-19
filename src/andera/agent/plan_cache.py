"""Plan cache — reuse plans across samples of the same task.

Key = sha256(task_prompt + canonical(schema) + url_pattern). On a
1000-sample run of one task, the planner LLM fires once; the other
999 samples read from disk. Dramatic cost + latency win.

Lives on the filesystem per the spec's "leverage file system" hint.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path("data/plan_cache")


def _canonical_json(obj: Any) -> str:
    """Deterministic JSON for hashing — sort keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _url_pattern(url: str | None) -> str:
    """Reduce a URL to its structural pattern.

    Replace numeric path segments with `:id` so
    `https://linear.app/foo/issue/ENG-1` and `...ENG-2` hash to the
    same pattern. Also strips querystring (not structural).
    """
    if not url:
        return ""
    base = url.split("?", 1)[0].rstrip("/")
    # replace any segment that's mostly digits OR looks like ID-123 form
    def sub(m: re.Match[str]) -> str:
        seg = m.group(0)
        if re.fullmatch(r"\d+", seg) or re.fullmatch(r"[A-Z]+-\d+", seg):
            return ":id"
        return seg
    return re.sub(r"[^/]+", sub, base)


def plan_key(task_prompt: str, schema: dict[str, Any], url: str | None) -> str:
    blob = "\x1f".join([
        task_prompt.strip(),
        _canonical_json(schema or {}),
        _url_pattern(url),
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PlanCache:
    def __init__(self, root: str | Path = DEFAULT_CACHE_DIR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> list[dict[str, Any]] | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text())
            if isinstance(data, list):
                return data
        except Exception:
            return None
        return None

    def put(self, key: str, plan: list[dict[str, Any]]) -> None:
        p = self._path(key)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False))
        tmp.rename(p)  # atomic
