"""Score + supersede ledgers v1 (self-improvement + data lifecycle, 2026-09-05).

score_ledger: one row per composed post {pid, rubric dimensions, total,
model tier, latency}. Written by the scorer (human or agent) after judging
against eval/SCENARIO_RUBRIC.md (dimensions generalize to all posts).

supersede_log: every provisional->finalized revision {indicator, period,
old_value, new_value, at}. Written by ingest paths when a fresh fetch
differs from a stored provisional row.
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

DB = "nexus_think_tank.db"


def ensure() -> None:
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS score_ledger(
        pid INTEGER, at TEXT, honesty REAL, thesis REAL, scenario_logic REAL,
        persian REAL, attribution REAL, hook REAL, total REAL,
        tier TEXT, notes TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS supersede_log(
        indicator TEXT, period TEXT, old_value REAL, new_value REAL,
        at TEXT)""")
    c.commit()
    c.close()


def record_score(pid: int, dims: dict, tier: str = "", notes: str = "") -> float:
    """dims: honesty/thesis/scenario_logic/persian/attribution/hook.
    Returns total. Weights = rubric maxes (2,2,2,2,1,1)."""
    from datetime import datetime
    ensure()
    weights = {"honesty": 2, "thesis": 2, "scenario_logic": 2,
               "persian": 2, "attribution": 1, "hook": 1}
    total = 0.0
    for k, w in weights.items():
        v = min(max(float(dims.get(k, 0)), 0), w)
        total += v
    c = sqlite3.connect(DB)
    c.execute("INSERT INTO score_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (pid, datetime.now().isoformat(timespec="seconds"),
               *[dims.get(k, 0) for k in weights], total, tier, notes))
    c.commit()
    c.close()
    return total


def log_supersede(conn: sqlite3.Connection, indicator: str, period: str,
                  old: float, new: float) -> None:
    from datetime import datetime
    conn.execute("INSERT INTO supersede_log VALUES (?,?,?,?,?)",
                 (indicator, period, old, new,
                  datetime.now().isoformat(timespec="seconds")))


if __name__ == "__main__":
    ensure()
    print("ledgers ready")
