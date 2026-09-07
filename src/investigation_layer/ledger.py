"""V0.2 -- editorial ledger: cross-post memory against monotony.

Stores the last N published posts' hook summary, format, insight kind, opening
phrase. Before composing, the composer gets an AVOID list; after publishing,
the runner appends a record. JSON file under var/, human-readable.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "..", "var", "editorial_ledger.json")
MAX_RECORDS = 12


def _load() -> list:
    try:
        with open(_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def record(format_name: str, hook_summary: str, insight_kind: str,
           opening_fa: str, problem_id: int) -> None:
    recs = _load()
    recs.append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "problem_id": problem_id,
        "format": format_name,
        "hook": hook_summary[:160],
        "insight_kind": insight_kind,
        "opening": opening_fa[:120],
    })
    with open(_PATH, "w", encoding="utf-8") as f:
        json.dump(recs[-MAX_RECORDS:], f, ensure_ascii=False, indent=1)


def avoid_block() -> str:
    """Compact avoid-list for the composer, or '' when ledger is empty."""
    recs = _load()[-8:]
    if not recs:
        return ""
    lines = ["EDITORIAL LEDGER (از تکرار اینها پرهیز کن — قلاب/شروع/زاویه مشابه ممنوع):"]
    for r in recs:
        lines.append(f"- [{r['date']}] P{r['problem_id']} fmt={r['format']} "
                     f"hook={r['hook']}")
        if r.get("opening"):
            lines.append(f"  opening: {r['opening']}")
    return "\n".join(lines)
