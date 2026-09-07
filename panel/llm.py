"""LLM route visibility + control (combo edition).

The pipeline's own src/investigation_layer/models.py is the source of truth
for the stage list; this module imports it rather than mirroring a copy.
Writes go to .env via set_env_key, which preserves every unrelated line
byte-for-byte and backs up .env into var/ first. Workers re-read env per
process, so changes take effect on the next run — the UI says so.

Probing costs a real (tiny) router call: button-press only, never page load.
Verdicts are shared with src/route_health (the ledger the pipeline consults
before every call). Secrets are read for probes but never rendered or logged.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import state

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ENV_F = ROOT / ".env"
HEALTH_F = ROOT / "var" / "route_health.json"

COMBOS = ["Mechanical", "Complicated", "MostComplicated", "Medium.Supporter"]


def read_env() -> dict:
    out = {}
    try:
        for line in ENV_F.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _pipeline_tasks() -> list[dict]:
    """The authoritative stage list, imported from the pipeline itself."""
    try:
        from src.investigation_layer.models import all_tasks
        return [{"task": t, "env": k, "default": d, "role": r}
                for t, k, d, r in all_tasks()]
    except Exception:
        return []


def effective_models() -> list[dict]:
    env = read_env()
    env.update({k: v for k, v in os.environ.items()
                if k.startswith("ROUTER_COMBO")})
    out = []
    for t in _pipeline_tasks():
        cur = env.get(t["env"], t["default"])
        out.append({**t, "current": cur, "overridden": t["env"] in env,
                    "key": t["task"]})
    return out


def fallback_chain() -> list[str]:
    env = read_env()
    raw = env.get("ROUTER_FALLBACK",
                  "MostComplicated,Complicated,Medium.Supporter,Mechanical")
    return [m.strip() for m in raw.split(",") if m.strip()]


def set_env_key(key: str, value: str) -> str:
    """Set one .env key, preserving all other lines. Backs up first."""
    if not re.match(r"^[A-Z_][A-Z0-9_]*$", key):
        return "Rejected: invalid key name."
    try:
        lines = ENV_F.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    bdir = ROOT / "var"
    bdir.mkdir(exist_ok=True)
    try:
        (bdir / f"env_backup_{stamp}").write_text(
            "\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    hit = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = f"{key}={value}"
            hit = True
            break
    if not hit:
        lines.append(f"{key}={value}")
    ENV_F.write_text("\n".join(lines) + "\n", encoding="utf-8")
    state.log_action("env-set", key, value[:120])
    return f"Saved {key}. Takes effect on the next pipeline run."


def _probe_model(model: str, timeout: float = 30.0) -> dict:
    """Tiny real call through the router. Returns {ok, latency_ms, error}."""
    env = read_env()
    base = env.get("ROUTER_BASE_URL") or os.environ.get("ROUTER_BASE_URL")
    key = env.get("ROUTER_API_KEY") or os.environ.get("ROUTER_API_KEY")
    if not base or not key:
        return {"ok": False, "latency_ms": None,
                "error": "ROUTER_BASE_URL / ROUTER_API_KEY not configured"}
    try:
        from openai import OpenAI
        client = OpenAI(base_url=base, api_key=key, timeout=timeout,
                        max_retries=0)
        t0 = time.monotonic()
        client.chat.completions.create(
            model=model, max_tokens=5,
            messages=[{"role": "user", "content": "Reply with the word OK."}])
        return {"ok": True, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": ""}
    except Exception as e:
        return {"ok": False, "latency_ms": None,
                "error": f"{type(e).__name__}: {str(e)[:160]}"}


def probe_tier(key: str) -> dict:
    """Probe one stage (by stage key, testing its current combo) or one
    combo directly (by combo name)."""
    if key in COMBOS:
        cur = key
    else:
        cur = next((u["current"] for u in effective_models()
                    if u["key"] == key), None)
    if cur is None:
        return {"ok": False, "error": "unknown stage or combo"}
    res = _probe_model(cur)
    state.save_route_status(key, cur, res)
    # Share the verdict with the pipeline's health ledger so a panel probe
    # immediately (un-)quarantines the combo for pipeline runs too.
    try:
        from src.route_health import record_ok, record_fail
        (record_ok if res.get("ok") else record_fail)(
            cur, res.get("error", ""), note="panel probe")
    except Exception:
        pass
    return res


def route_statuses() -> dict:
    return state.get_route_statuses()


def health_ledger() -> dict:
    """The pipeline's route_health.json, read-only."""
    try:
        return json.loads(HEALTH_F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def set_route_enabled(route: str, enabled: bool, hours: float = 12.0) -> str:
    """Panel button: quarantine/re-enable any route or combo in the SHARED
    ledger — the pipeline honors this on every call from now on."""
    from src import route_health
    msg = route_health.set_manual(route, not enabled, hours=hours,
                                  note="panel")
    state.log_action("route-quarantine" if not enabled else "route-enable",
                     route, msg)
    return msg


def set_stage_combo(task: str, combo: str) -> str:
    """Panel button: assign a combo to a pipeline stage (writes .env)."""
    stage = next((u for u in effective_models() if u["key"] == task), None)
    if stage is None:
        return "Nothing done — unknown pipeline stage."
    if combo not in COMBOS:
        return "Nothing done — unknown combo."
    msg = set_env_key(stage["env"], combo)
    state.log_action("stage-assign", task, f"{stage['env']}={combo}")
    return msg
