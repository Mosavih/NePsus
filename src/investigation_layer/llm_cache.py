"""Deterministic LLM-output cache (closes LLM-variance residual of D5).

Keyed by sha256(model + system + user). On a miss, calls the router and stores
the raw text; on a hit, returns the cached text. This makes eval runs on the
frozen corpus fully REPRODUCIBLE: the same (model, prompt, study) always yields
the same verdict/finding, so gate-eval and pipeline audits no longer drift
between runs (temperature=0 is not perfectly honored by the router/model).

Cache is a single JSONL file (append-only, rerun-safe). Set LLM_CACHE=off env to
disable (e.g. for production freshness). Default: ON.
"""
import os
import json
import hashlib
import threading

_CACHE_PATH = os.path.join(os.path.dirname(__file__), "llm_cache.jsonl")
_lock = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("LLM_CACHE", "on").lower() != "off"


def _key(model: str, system: str, user: str) -> str:
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x00")
    h.update(system.encode("utf-8"))
    h.update(b"\x00")
    h.update(user.encode("utf-8"))
    return h.hexdigest()


def _load() -> dict:
    if not os.path.exists(_CACHE_PATH):
        return {}
    out = {}
    with open(_CACHE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                out[rec["key"]] = rec["text"]
            except Exception:
                continue
    return out


_CACHE = None


def get(model: str, system: str, user: str):
    """Return cached text for (model, system, user), or None on miss."""
    if not _enabled():
        return None
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE.get(_key(model, system, user))


def put(model: str, system: str, user: str, text: str) -> None:
    if not _enabled():
        return
    key = _key(model, system, user)
    with _lock:
        # update in-memory
        global _CACHE
        if _CACHE is None:
            _CACHE = _load()
        _CACHE[key] = text
        # append to file
        with open(_CACHE_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"key": key, "text": text}, ensure_ascii=False) + "\n")


def stats() -> dict:
    if not _enabled():
        return {"enabled": False}
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return {"enabled": True, "entries": len(_CACHE), "path": _CACHE_PATH}
