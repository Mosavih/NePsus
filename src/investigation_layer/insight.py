"""V0.2 -- insight pass: find the NON-OBVIOUS before composing.

The step-back review's core finding: V0.1 posts are correct, honest, coherent --
and rarely surprising. This module runs BEFORE composition:

  1. propose_insights(): LLM proposes 2-3 candidate "sharp claims" strictly
     derivable from the dossier text (comparison / tension / implication /
     counter-intuitive trend), each with a one-line derivation.
  2. validate: mechanical checks -- every number in a claim must appear in the
     dossier (reuses reviewer.verify_numbers machinery); claim must be <= 240 chars.
  3. pick: the composer prompt receives the valid claims and must build the
     narrative spine around ONE of them (or explicitly argue why none holds).

This is the difference between reporting data and saying something.
"""
from __future__ import annotations

import json
import re

from src.investigation_layer.reviewer import (
    _client, verify_numbers,
)

INSIGHT_SYS = (
    "You are a sharp research editor. Given an evidence dossier about an Iran "
    "problem, propose the most INTERESTING claims that are strictly derivable "
    "from it. Prefer tension, contrast, counter-intuition, implications for "
    "ordinary people. Never invent numbers; only rearrange or juxtapose what "
    "is in the dossier. Respond ONLY with JSON."
)

INSIGHT_USR = """Evidence dossier:
--- start ---
{dossier}
--- end ---

Propose 3 candidate insights (sharp claims) for a Persian-language Telegram post
for a general audience. Each claim must be:
- strictly derivable from the dossier (every number traceable)
- non-obvious (not just restating one statistic)
- concrete (name entities, years, directions)

Respond ONLY with JSON:
{{
  "insights": [
    {{"claim": "...one sentence English...",
      "kind": "contrast|tension|implication|counterintuitive",
      "derivation": "which dossier facts, combined how"}},
    ... up to 3 ...
  ]
}}"""


def propose_insights(dossier_text: str, model: str | None = None,
                     pace: float = 12.0) -> list[dict]:
    import time as _t
    from src.route_health import chat as _rchat
    if pace:
        _t.sleep(pace)
    raw, _used = _rchat("insight",
        messages=[{"role": "system", "content": INSIGHT_SYS},
                  {"role": "user", "content": INSIGHT_USR.format(
                      dossier=dossier_text[:14000])}],
        temperature=0.6, model=model)
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out = []
    for it in (data.get("insights") or [])[:3]:
        if isinstance(it, dict) and it.get("claim"):
            out.append({
                "claim": str(it["claim"])[:400],
                "kind": str(it.get("kind") or "implication"),
                "derivation": str(it.get("derivation") or "")[:400],
            })
    return out


def validate_insights(insights: list[dict], dossier_text: str) -> list[dict]:
    """Mechanical gate: numbers in a claim must exist in the dossier."""
    valid = []
    for ins in insights:
        c = ins["claim"]
        if len(c) > 320:
            continue
        guard = verify_numbers(c, dossier_text)
        if guard.get("ok"):
            valid.append(ins)
    return valid


def format_insight_block(valid: list[dict]) -> str:
    if not valid:
        return ""
    lines = [
        "INSIGHT CANDIDATES (ادعاهای تیزِ تأییدشده — پست باید ستون فقرات روایت را حول",
        "یکی از اینها بسازد؛ اگر هیچ‌کدام را نمی‌پذیری، صریح بگو چرا):",
    ]
    for i, v in enumerate(valid, 1):
        lines.append(f"{i}. [{v['kind']}] {v['claim']}")
        lines.append(f"   derivation: {v['derivation']}")
    return "\n".join(lines)
