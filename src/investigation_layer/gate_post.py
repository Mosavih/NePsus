"""Step 51 -- Engaging / accurate post gate.

Scores a rendered post (Step 50) on two axes and returns a decision:
  PASS    : accurate AND engaging enough to publish
  REVISE  : accurate but weak engagement (e.g. all-gaps, too long) -> fix
  HOLD    : inaccurate or unsafe (e.g. no evidence cited, implies 'solved')

Accuracy (deterministic, from the post + its source data):
  - at least one evidence id is cited (measurement/finding) OR the post is
    explicitly 'evidence pending' (honest, not fabricated)
  - it must NOT claim a definitive solution when confidence=indicative
  - gaps section present

Engagement (hybrid):
  - deterministic structure checks: hook present, diagram present, length in
    [min,max], has the 3 sections
  - optional LLM engagement score (0-1) when a client is supplied; otherwise a
    deterministic proxy (has hook + diagram + <= N words + lever or evidence)

No post is ever auto-published; this gate only recommends. The caller (Step 53)
decides on delivery.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict


@dataclass
class PostGateResult:
    decision: str            # PASS | REVISE | HOLD
    accuracy_ok: bool
    engagement_score: float  # 0-1
    reasons: list
    accurate_reasons: list
    engage_reasons: list

    def to_dict(self) -> dict:
        return asdict(self)


# Deterministic thresholds.
MIN_WORDS = 40
MAX_WORDS = 600
REQUIRED_SECTIONS = ["What the evidence says", "How it could be addressed",
                     "What we don't yet know"]


def _count_evidence_ids(markdown: str) -> int:
    return len(re.findall(r"\[(measurement|finding):\d+\]", markdown))


def gate_post(post: dict, sa_confidence: str = "grounded",
              client=None, model: str | None = None) -> PostGateResult:
    md = post.get("markdown", "")
    reasons = []
    acc_reasons = []
    eng_reasons = []

    # ---- Accuracy ----
    accuracy_ok = True
    n_ev = _count_evidence_ids(md)
    pending = "Evidence pending" in md
    if n_ev == 0 and not pending:
        accuracy_ok = False
        acc_reasons.append("HOLD: no evidence cited and not labeled 'evidence pending' (fabrication risk)")
    if sa_confidence == "indicative" and re.search(r"\b(solved|resolved|will fix|guaranteed)\b", md, re.I):
        accuracy_ok = False
        acc_reasons.append("HOLD: implies definitive solution while evidence is indicative only")
    if "## 🔍 What we don't yet know" not in md:
        accuracy_ok = False
        acc_reasons.append("HOLD: missing gaps/honesty section")
    if accuracy_ok and not acc_reasons:
        acc_reasons.append("accurate: evidence cited or honestly pending; gaps shown")

    # ---- Engagement (deterministic, substance-weighted) ----
    # Structure is necessary but not sufficient: a public Telegram post must
    # carry REAL evidence + a concrete lever to be engaging, not just a diagram
    # placeholder over "evidence pending". Substance dominates the score.
    words = len(md.split())
    has_hook = "Iran" in md[:200]
    has_diagram = "```" in md and ("PROBLEM:" in md or "flowchart" in md)
    has_sections = all(s in md for s in REQUIRED_SECTIONS)
    n_ev = _count_evidence_ids(md)
    has_lever = "How it could be addressed" in md and (
        "finding" in md or "Intervention" in md or "lever" in md.lower())

    eng = 0.0
    # Substance (0.6 max): the part that actually makes a post worth reading.
    if n_ev > 0:
        eng += min(0.4, 0.08 * n_ev)          # cited evidence -> engaging
    else:
        eng_reasons.append("no cited evidence (low substance)")
    if sa_confidence == "grounded":
        eng += 0.2                              # a real, evidence-grounded lever
    else:
        eng_reasons.append("no evidence-grounded lever (indicative only)")
    # Structure (0.4 max): necessary scaffolding.
    if MIN_WORDS <= words <= MAX_WORDS:
        eng += 0.1
    else:
        eng_reasons.append(f"word count {words} outside [{MIN_WORDS},{MAX_WORDS}]")
    if has_hook:
        eng += 0.1
    else:
        eng_reasons.append("no Iran hook")
    if has_diagram:
        eng += 0.1
    else:
        eng_reasons.append("no diagram")
    if has_sections:
        eng += 0.1
    else:
        eng_reasons.append("missing required sections")

    # Optional LLM engagement refinement (only when structure+substance pass).
    if client is not None and eng >= 0.6:
        try:
            from .extraction import _chat, _parse_json_block
            prompt = (
                "Rate how engaging this Telegram post is for a general, "
                "non-academic audience (0.0-1.0). Return ONLY JSON "
                "{\"score\": float, \"why\": str}.\n\n" + md[:1500]
            )
            raw = _chat(
                "You are an editorial engagement reviewer for a science-policy "
                "Telegram channel.", prompt, temperature=0)
            d = _parse_json_block(raw)
            llm = float(d.get("score", eng))
            eng = round(0.5 * eng + 0.5 * max(0.0, min(1.0, llm)), 2)
            eng_reasons.append("LLM engagement refinement applied")
        except Exception:
            eng_reasons.append("LLM engagement score unavailable; used proxy")

    eng = round(eng, 2)
    if not eng_reasons:
        eng_reasons.append("engagement structure OK")

    # ---- Decision ----
    if not accuracy_ok:
        decision = "HOLD"
        reasons.append("accuracy failed -> HOLD")
    elif eng >= 0.7:
        decision = "PASS"
        reasons.append(f"accurate + engaging (score {eng}) -> PASS")
    else:
        decision = "REVISE"
        reasons.append(f"accurate but engagement {eng} < 0.7 -> REVISE")

    return PostGateResult(decision, accuracy_ok, eng, reasons, acc_reasons, eng_reasons)
