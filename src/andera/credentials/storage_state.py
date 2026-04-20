"""AES-GCM sealed Playwright storage_state.

Threat model:
  - .env is gitignored. ANDERA_MASTER_KEY lives there.
  - Sealed state files can be committed (encrypted); they are useless
    without the master key.
  - Same-origin cookies only — we do not try to hide HTTP data.

Layout on disk:
  data/credentials/<host>.sealed   (base64-encoded nonce + ciphertext)

Key derivation: HKDF(master_bytes, info=b"andera/storage-state")
  - Master key is ENV var base64-decoded to raw bytes, OR a UTF-8
    string that HKDF-expands into 32 bytes.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12
KEY_BYTES = 32
ENV_VAR = "ANDERA_MASTER_KEY"
INFO = b"andera/storage-state"


def derive_key_from_env(env_var: str = ENV_VAR) -> bytes:
    """Return a 32-byte AES key derived from the master key env var."""
    raw = os.environ.get(env_var)
    if not raw:
        raise RuntimeError(
            f"{env_var} is not set. Generate one with "
            "`python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\"` "
            "and put it in .env."
        )
    # Accept either base64 or plain utf-8 passphrase
    try:
        material = base64.b64decode(raw.encode("ascii"), validate=True)
    except Exception:
        material = raw.encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=INFO).derive(material)


def seal(plaintext: bytes, key: bytes) -> bytes:
    """Return base64(nonce || ciphertext_with_tag)."""
    if len(key) != KEY_BYTES:
        raise ValueError("key must be 32 bytes")
    aes = AESGCM(key)
    nonce = os.urandom(NONCE_BYTES)
    ct = aes.encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ct)


def unseal(sealed: bytes, key: bytes) -> bytes:
    blob = base64.b64decode(sealed)
    nonce, ct = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
    return AESGCM(key).decrypt(nonce, ct, None)


class SealedStateStore:
    """Per-host sealed Playwright storage_state store."""

    def __init__(self, root: str | Path = "data/credentials", env_var: str = ENV_VAR) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._env_var = env_var

    def _path(self, host: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in host)
        return self.root / f"{safe}.sealed"

    def has(self, host: str) -> bool:
        return self._path(host).exists()

    def save(self, host: str, state: dict[str, Any]) -> Path:
        key = derive_key_from_env(self._env_var)
        sealed = seal(json.dumps(state, separators=(",", ":")).encode("utf-8"), key)
        p = self._path(host)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(sealed)
        tmp.rename(p)
        return p

    def load(self, host: str) -> dict[str, Any] | None:
        p = self._path(host)
        if not p.exists():
            return None
        key = derive_key_from_env(self._env_var)
        return json.loads(unseal(p.read_bytes(), key).decode("utf-8"))

    def delete(self, host: str) -> bool:
        p = self._path(host)
        if p.exists():
            p.unlink()
            return True
        return False

    def list_hosts(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.sealed"))

    def load_merged(self) -> dict[str, Any] | None:
        """Merge every sealed host into a single Playwright storage_state.

        Multi-host tasks (e.g. start on GitHub, hop to Google then
        LinkedIn) need ALL sealed sessions loaded into the browser
        context, not just the start_url's. Cookies are domain-scoped
        by the browser so merging is safe — each host only ever sees
        its own cookies + localStorage.

        Returns None when no hosts are sealed OR the master key is
        missing (in which case every per-host load would raise anyway).
        """
        hosts = self.list_hosts()
        if not hosts:
            return None
        merged: dict[str, Any] = {"cookies": [], "origins": []}
        loaded_any = False
        for h in hosts:
            try:
                state = self.load(h)
            except Exception:
                continue
            if not state:
                continue
            merged["cookies"].extend(state.get("cookies") or [])
            merged["origins"].extend(state.get("origins") or [])
            loaded_any = True
        return merged if loaded_any else None
