"""Route health ledger — shared by pipeline and panel.

ONE truth file: var/route_health.json
  { "<model-or-alias>": {"fails": int, "last_fail": iso, "last_ok": iso,
                          "error": str, "disabled_until": iso|None, "note": str} }

Quarantine policy (the user's standing rule, codified 2026-09-06):
a route that fails twice in a row is quarantined for 30 min; each further
consecutive failure doubles the quarantine (max 12 h). A success clears it.
Callers MUST check `usable()` before dialing a route and `record_*()` after —
this is what stops the endless-grind-against-a-dead-provider disease.

Also ships call_llm_resilient(): walk candidate routes, skip quarantined ones,
record health, return the first answer. Gate2/discover/reviewer all use it.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEALTH_F = ROOT / "var" / "route_health.json"

# Router combo aliases (local router :20128). The pipeline now assigns one
# combo per task (see src/investigation_layer/models.py); the router walks
# providers inside each combo. Health is tracked per combo-alias here.
FALLBACK_ALIASES = ["C1", "opncde"]

COMBO_FALLBACK_ORDER = ["Complicated", "Medium.Supporter",
                        "Mechanical", "MostComplicated"]


def combo_candidates(primary: str) -> list[str]:
    """[primary combo] + other combos as last-resort, deduped."""
    out, seen = [primary], {primary}
    for c in COMBO_FALLBACK_ORDER:
        if c not in seen:
            out.append(c)
            seen.add(c)
    return out

BASE_QUARANTINE_S = 1800        # 30 min
MAX_QUARANTINE_S = 12 * 3600    # 12 h


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load() -> dict:
    try:
        return json.loads(HEALTH_F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    HEALTH_F.parent.mkdir(exist_ok=True)
    HEALTH_F.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")


def _entry(data: dict, route: str) -> dict:
    return data.setdefault(route, {"fails": 0, "last_fail": None,
                                   "last_ok": None, "error": "",
                                   "disabled_until": None, "note": ""})


def usable(route: str) -> bool:
    """False only while actively quarantined."""
    e = _load().get(route)
    if not e or not e.get("disabled_until"):
        return True
    try:
        until = datetime.fromisoformat(e["disabled_until"])
    except ValueError:
        return True
    return _now() >= until


def record_fail(route: str, error: str = "", note: str = "") -> None:
    data = _load()
    e = _entry(data, route)
    e["fails"] = int(e.get("fails", 0)) + 1
    e["last_fail"] = _now().isoformat(timespec="seconds")
    e["error"] = str(error)[:200]
    if note:
        e["note"] = note[:200]
    if e["fails"] >= 2:
        wait = min(BASE_QUARANTINE_S * (2 ** (e["fails"] - 2)), MAX_QUARANTINE_S)
        e["disabled_until"] = (_now() + timedelta(seconds=wait)).isoformat(timespec="seconds")
    _save(data)


def record_ok(route: str) -> None:
    data = _load()
    e = _entry(data, route)
    e["fails"] = 0
    e["last_ok"] = _now().isoformat(timespec="seconds")
    e["disabled_until"] = None
    e["error"] = ""
    _save(data)


def set_manual(route: str, disabled: bool, hours: float = 12.0, note: str = "") -> str:
    """Panel button: force-disable or re-enable a route immediately."""
    data = _load()
    e = _entry(data, route)
    if disabled:
        e["disabled_until"] = (_now() + timedelta(hours=hours)).isoformat(timespec="seconds")
        if not e["fails"]:
            e["fails"] = 2  # keep doubling logic consistent
        if note:
            e["note"] = note[:200]
        _save(data)
        return f"{route} disabled for {hours:g} h."
    e["disabled_until"] = None
    e["fails"] = 0
    if note:
        e["note"] = note[:200]
    _save(data)
    return f"{route} re-enabled."


def snapshot() -> dict:
    return _load()


def healthy_candidates(candidates: list[str]) -> list[str]:
    """Quarantined routes go last, not gone — if everything is dead we still
    try the freshest quarantine rather than refusing outright."""
    ok = [c for c in candidates if usable(c)]
    dead = [c for c in candidates if not usable(c)]
    return ok + dead


def call_llm_resilient(build_client, messages, candidates: list[str],
                       temperature: float = 0.0, timeout: float = 120.0,
                       max_tokens: int | None = None, **kw):
    """Walk candidates, skipping quarantined routes; return (text, route).

    build_client(route) -> OpenAI-compatible client. Records health for every
    attempt. Raises RuntimeError only if every candidate failed.
    """
    errors = []
    for route in healthy_candidates(candidates):
        try:
            client = build_client(route, timeout)
            kwargs = dict(model=route, messages=messages,
                          temperature=temperature)
            if max_tokens:
                kwargs["max_tokens"] = max_tokens
            resp = client.chat.completions.create(**kwargs)
            text = (resp.choices[0].message.content or "").strip()
            record_ok(route)
            return text, route
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:160]}"
            record_fail(route, msg)
            errors.append(f"{route}: {msg}")
    raise RuntimeError("all routes failed —\n" + "\n".join(errors))


def _default_client(route: str, timeout: float):
    from openai import OpenAI
    base = os.environ.get("ROUTER_BASE_URL", "http://localhost:20128/v1")
    key = os.environ.get("ROUTER_API_KEY", "")
    return OpenAI(base_url=base, api_key=key, timeout=timeout, max_retries=0)


def chat(task: str, messages: list, temperature: float = 0.0,
         timeout: float | None = None, max_tokens: int | None = None,
         model: str | None = None, **kw):
    """One shared LLM entry point: task -> combo -> resilient walk.

    Resolves combo_for(task) at CALL time (panel Assign takes effect on the
    next run), walks combo_candidates (siblings as last resort), records
    health per combo. An explicit model (provider route or combo) goes
    FIRST, then the assigned combo's candidates. Returns (text, used).
    """
    from src.investigation_layer.models import combo_for
    primary = combo_for(task)
    sibs = combo_candidates(primary)
    cands = ([model] if model and model not in set(sibs) else []) + sibs
    cands = cands + [a for a in FALLBACK_ALIASES if a not in set(cands)]
    return call_llm_resilient(_default_client, messages, cands,
                              temperature=temperature,
                              timeout=timeout or 120.0,
                              max_tokens=max_tokens, **kw)
