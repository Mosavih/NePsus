"""Central LLM tier map — COMBO EDITION (2026-09-06).

The local router (:20128) exposes four user-curated combo aliases. Each combo
is a multi-provider fallback group managed INSIDE the router, so the pipeline
no longer hardcodes provider routes: it assigns a COMBO per task and lets the
router walk providers. Route health (src/route_health.py) still tracks each
combo-alias so a combo that stops answering is skipped, not ground on.

  Mechanical      fastest/cheapest, mechanical work (JSON, gating, keywords)
  Complicated     smarter reasoning models (drafting, insight, review)
  MostComplicated strongest model (gemini-3.7-flash) — limited access, high
                  demand, long delays: compose only, never bulk work
  Medium.Supporter  middle ground; the fragility cushion — covers mechanical
                  and light-complicated work when others are exhausted

Every assignment is env-overridable (ROUTER_COMBO_<TASK>) and re-read at call
time. The panel writes these env keys; nothing else needs to change.
"""
from __future__ import annotations

import os

COMBOS = ["Mechanical", "Complicated", "MostComplicated", "Medium.Supporter"]

# task -> (env key, default combo, role)
TASKS: dict[str, tuple[str, str, str]] = {
    # gate2 artifact extraction: bulk, JSON, high volume
    "extract":    ("ROUTER_COMBO_EXTRACT", "Mechanical",
                   "artifact extraction (gate2) — bulk JSON"),
    # gate3.5 events: bulk JSON
    "events":     ("ROUTER_COMBO_EVENTS", "Mechanical",
                   "event detection (gate3.5) — bulk JSON"),
    # gate4 priority / keyword jobs
    "fast":       ("ROUTER_COMBO_FAST", "Mechanical",
                   "fast jobs: keywords, gating, mechanical"),
    # discovery: propose problems/questions
    "draft":      ("ROUTER_COMBO_DRAFT", "Complicated",
                   "problem & question drafting, insights"),
    # query expansion for retrieval
    "expand":     ("ROUTER_COMBO_EXPAND", "Medium.Supporter",
                   "retrieval query expansion"),
    # post writing — quality-critical, uses the strongest when needed
    "compose":    ("ROUTER_COMBO_COMPOSE", "MostComplicated",
                   "post writing — quality-critical"),
    # review + final QC of composed posts
    "review":     ("ROUTER_COMBO_REVIEW", "Complicated",
                   "review + final QC proofread"),
    # insight proposals inside compose flow
    "insight":    ("ROUTER_COMBO_INSIGHT", "Medium.Supporter",
                   "insight proposals (compose flow)"),
    # stat-card copy
    "cards":      ("ROUTER_COMBO_CARDS", "Medium.Supporter",
                   "stat-card copy"),
    # concept/visual prompts
    "visual":     ("ROUTER_COMBO_VISUAL", "Medium.Supporter",
                   "visual concept prompts"),
    # reader questions -> problems
    "ask":        ("ROUTER_COMBO_ASK", "Medium.Supporter",
                   "reader questions to problems"),
    # synthesis jobs
    "synthesis":  ("ROUTER_COMBO_SYNTHESIS", "Complicated",
                   "synthesis jobs"),
}

# Legacy task names kept working: old callers pass fast/draft/compose.
_LEGACY = {"fast": "fast", "draft": "draft", "compose": "compose"}


def _default_combo(task: str) -> str:
    row = TASKS.get(task)
    if row:
        return row[1]
    legacy = {"fast": "Mechanical", "draft": "Complicated",
              "compose": "MostComplicated"}
    return legacy.get(task, "Medium.Supporter")


def combo_for(task: str) -> str:
    """Task -> combo alias. Env-overridable per task, re-read at call time."""
    if task in TASKS:
        key = TASKS[task][0]
        return os.environ.get(key, TASKS[task][1])
    return os.environ.get(f"ROUTER_COMBO_{task.upper()}",
                          _default_combo(task))


def env_key_for(task: str) -> str:
    row = TASKS.get(task)
    if row:
        return row[0]
    return f"ROUTER_COMBO_{task.upper()}"


def role_for(task: str) -> str:
    row = TASKS.get(task)
    return row[2] if row else "misc"


def all_tasks() -> list[tuple[str, str, str, str]]:
    """[(task, env_key, default_combo, role)] in stable order."""
    return [(t, row[0], row[1], row[2]) for t, row in TASKS.items()]


# ---- back-compat: model_for(task) now returns the combo alias -------------
_TIERS = {}


def model_for(task: str) -> str:
    return combo_for(task)


def pace_for(task: str) -> float:
    key = {"fast": "ROUTER_PACE_FAST", "draft": "ROUTER_PACE_DRAFT",
           "compose": "ROUTER_PACE_COMPOSE"}.get(task,
                                                 f"ROUTER_PACE_{task.upper()}")
    return float(os.environ.get(key, 12.0))


CALL_TIMEOUT_S = float(os.environ.get("ROUTER_CALL_TIMEOUT", "300"))

FALLBACK_CHAIN = [c for c in (
    os.environ.get("ROUTER_FALLBACK",
                   "MostComplicated,Complicated,Medium.Supporter,Mechanical")
    .split(",")) if c.strip()]
