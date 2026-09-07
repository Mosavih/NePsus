"""Step 19 — Question-scoped Evidence Relationship Layer (minimal).

Makes the semantic relationship between two pieces of evidence EXPLICIT and
QUERYABLE, without redesigning synthesis, without an LLM prose generator, and
without forecasting/transferability.

Design (see STRATEGY §35):
- Relation identity INCLUDES the Question (Option B): the same evidence pair
  can legitimately bear a different relationship under a different Question, and
  rerun-safety requires per-Question uniqueness.
- Vocabulary is minimal and empirically justified:
    agree | conflict | address_different_aspect | not_comparable
  ('agree' and 'corroborate' were collapsed — not reliably distinguishable from
  the existing Finding structure; 'conflict' is a single relation covering
  opposite direction / incompatible factual / incompatible causal claims).
- Candidate-pair rule is NOT O(n^2) over everything: only same-Question pairs
  that share a demonstrated semantic overlap become candidates:
    * two FINDINGS with the same non-empty `intervention_name`, OR
    * two MEASUREMENTS with DIFFERENT indicators (one representative pair per
      indicator pair, not per time-point -- a time series is one object).
  Finding<->Measurement (mixed) pairs are deliberately NOT related in Step 19
  (Step 8: do not force Measurements into the Finding relation model; report
  rather than generalize).
- The classifier is model-assisted but NARROW: input is only persisted evidence
  already linked to the Question; it must NOT retrieve, rewrite findings, or
  generate an answer. It MAY abstain with `not_comparable`.
- `conflict` never implies a winner; the classifier is forbidden from concluding
  which study is correct.

No synthesis is performed: this module only writes evidence_relation rows.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .extraction import _chat

RELATIONS = {"agree", "conflict", "address_different_aspect", "not_comparable"}

_REL_SYS = (
    "You are a relationship classifier for an evidence system. You are given ONE "
    "Investigation Question and exactly TWO pieces of evidence already linked to "
    "it (no other context). Your ONLY job is to state how these two pieces of "
    "evidence relate WITH RESPECT TO THAT QUESTION.\n"
    "Allowed relations:\n"
    " - 'conflict': the two pieces make incompatible claims (opposite direction / "
    "opposite outcome / incompatible factual or causal claims) about the SAME "
    "question. NOTE: conflict means the claims disagree -- it does NOT mean either "
    "is correct or wrong, and you must NOT conclude which study wins, whether a "
    "policy 'works' or 'fails', or assign any verdict.\n"
    " - 'agree': the two pieces co-support the same claim / same direction.\n"
    " - 'address_different_aspect': the two pieces answer DIFFERENT sub-questions "
    "or measure different variables, so they are not in conflict even if their "
    "numbers move in opposite directions.\n"
    " - 'not_comparable': the evidence does not contain enough semantic information "
    "to establish any of the above (ABSTAIN -- prefer this over a forced label).\n"
    "Return ONLY JSON: "
    '{"relation": "conflict"|"agree"|"address_different_aspect"|"not_comparable", '
    '"justification": str, "evidence_references": [str], "question_reference": str}'
    "\nJustification must use ONLY the supplied evidence text -- never introduce "
    "external facts. Do not write a synthesis or answer the Question."
)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            return {}
        try:
            return json.loads(text[s:e + 1])
        except json.JSONDecodeError:
            return {}


def _evidence_text(db, etype: str, eid: int) -> str:
    """Render a persisted evidence record as plain text for the classifier."""
    conn = db._require_connection()
    if etype == "finding":
        r = conn.execute(
            "SELECT statement, population, outcome, intervention_name, "
            "causal_strength, limitations FROM findings WHERE id=?",
            (eid,),
        ).fetchone()
        if not r:
            return ""
        parts = [f"FINDING: {r['statement']}"]
        if r["intervention_name"]:
            parts.append(f"intervention/claim: {r['intervention_name']}")
        if r["outcome"]:
            parts.append(f"outcome: {r['outcome']}")
        if r["causal_strength"]:
            parts.append(f"strength: {r['causal_strength']}")
        if r["limitations"]:
            parts.append(f"limitations: {r['limitations']}")
        return " | ".join(parts)
    else:  # measurement
        r = conn.execute(
            "SELECT indicator, subject_entity_name, value, unit, reference_period "
            "FROM measurements WHERE id=?",
            (eid,),
        ).fetchone()
        if not r:
            return ""
        val = r["value"]
        try:
            val = f"{float(val):,.2f}"
        except (TypeError, ValueError):
            pass
        return (f"MEASUREMENT: {r['indicator']} | {r['subject_entity_name'] or ''} "
                f"| {r['reference_period']}: {val} {r['unit'] or ''}".strip())


def candidate_pairs(db, question_id: int) -> list:
    """Enumerate same-Question evidence pairs that share a demonstrated overlap.

    Rule (minimal, no O(n^2) over unrelated evidence):
      - two FINDINGS sharing the same non-empty `intervention_name` (all pairs),
      - two MEASUREMENTS with DIFFERENT indicators (one representative pair per
        indicator pair, using the latest reference_period of each).
    Finding<->Measurement pairs are excluded (mixed substrate deferred in Step 19).
    Same-indicator measurement points (a time series) are excluded (one object).
    """
    conn = db._require_connection()
    rows = conn.execute(
        "SELECT evidence_type, evidence_id FROM question_evidence "
        "WHERE question_id=?",
        (question_id,),
    ).fetchall()

    findings = []   # (id, intervention_name, outcome)
    meas_by_ind = {}  # indicator -> {source: (id, reference_period)}
    for r in rows:
        et, eid = r["evidence_type"], r["evidence_id"]
        if et == "finding":
            f = conn.execute(
                "SELECT intervention_name, outcome FROM findings WHERE id=?",
                (eid,)).fetchone()
            iv = (f["intervention_name"] or "").strip() if f else ""
            out = (f["outcome"] or "").strip() if f else ""
            findings.append((eid, iv, out))
        else:
            m = conn.execute(
                "SELECT indicator, reference_period FROM measurements WHERE id=?",
                (eid,)).fetchone()
            if not m:
                continue
            ind = m["indicator"]
            rp = m["reference_period"] or ""
            meas_by_ind.setdefault(ind, {})[eid] = rp

    pairs = []
    # Findings sharing the SAME non-empty intervention_name OR the SAME non-empty
    # outcome. (rationale: same intervention -> direct comparison; same outcome
    # across DIFFERENT interventions -> "common effect" comparison. Both are
    # deterministic, semantically predictive signals; no O(n^2) over all findings.)
    by_key = {}
    for eid, iv, out in findings:
        for key in (("iv", iv), ("out", out)):
            if key[1]:
                by_key.setdefault(key, []).append(eid)
    for key, ids in by_key.items():
        # dedupe ids that may appear under both iv and out
        seen = set()
        uniq = []
        for i in ids:
            if i not in seen:
                seen.add(i); uniq.append(i)
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                pairs.append(("finding", uniq[i], "finding", uniq[j]))
    # Measurements. Build per-indicator representative (id, period) list.
    ind_items = {}  # indicator -> list of (id, period)
    for ind, src_map in meas_by_ind.items():
        items = []
        for mid, rp in src_map.items():
            items.append((mid, rp))
    # Measurements. Build per-indicator list of (id, period, source).
    ind_items = {}  # indicator -> list of (id, period, source)
    for ind, src_map in meas_by_ind.items():
        items = []
        for mid, rp in src_map.items():
            s = conn.execute(
                "SELECT measurement_source FROM measurements WHERE id=?",
                (mid,)).fetchone()["measurement_source"] or ""
            items.append((mid, rp, s))
        ind_items[ind] = items
    # (b) SAME indicator, DIFFERENT source, SAME period -> pair the cross-source
    #     values for each shared period (captures source disagreement, e.g. WDI vs
    #     IMF GDP for 2022). This never pairs a time-series point against itself
    #     (same indicator + same source) and keeps the comparison period-aligned.
    for ind, items in ind_items.items():
        by_period = {}
        for mid, rp, s in items:
            by_period.setdefault(rp, {})[s] = mid
        for rp, src_map in by_period.items():
            srcs = list(src_map.values())
            for i in range(len(srcs)):
                for j in range(i + 1, len(srcs)):
                    pairs.append(("measurement", srcs[i], "measurement", srcs[j]))
    # (a) DIFFERENT indicators -> one representative (latest-period) pair. Useful
    #     macro comparison (e.g. exports vs GDP); classifier abstains on arbitrary
    #     pairs, so precision is recovered at classification time.
    inds = list(ind_items.keys())
    for i in range(len(inds)):
        for j in range(i + 1, len(inds)):
            a = max(ind_items[inds[i]], key=lambda x: x[1])[0]
            b = max(ind_items[inds[j]], key=lambda x: x[1])[0]
            pairs.append(("measurement", a, "measurement", b))
    return pairs


def classify_relation(question_text: str, text_a: str, text_b: str,
                     model: str | None = None) -> dict:
    """Model-assisted classification. Returns a normalized dict.

    Never retrieves, never rewrites findings, never generates an answer.
    On any error returns not_comparable (safe abstention).
    """
    user = (
        f"QUESTION: {question_text}\n\n"
        f"EVIDENCE A:\n{text_a}\n\n"
        f"EVIDENCE B:\n{text_b}\n\n"
        "Classify the relationship (conflict | agree | address_different_aspect | "
        "not_comparable) and justify using ONLY the evidence above."
    )
    try:
        raw = _chat(_REL_SYS, user, temperature=0)
    except Exception as e:  # noqa: BLE001
        return {"relation": "not_comparable", "justification": f"classifier error: {e}",
                "evidence_references": [], "question_reference": question_text}
    out = _parse_json(raw)
    rel = out.get("relation")
    if rel not in RELATIONS:
        rel = "not_comparable"
    return {
        "relation": rel,
        "justification": out.get("justification", ""),
        "evidence_references": out.get("evidence_references", []) or [],
        "question_reference": out.get("question_reference", question_text),
    }


def relate_question_evidence(db, question_id: int, question_text: str,
                             use_model: bool = True, model: str | None = None) -> int:
    """Compute and persist Question-scoped evidence relationships.

    Returns the number of relation rows written. Rerun-safe (INSERT OR IGNORE;
    (a,b) normalized). Does NOT modify the Question answer.
    """
    pairs = candidate_pairs(db, question_id)
    written = 0
    for (ta, a, tb, b) in pairs:
        text_a = _evidence_text(db, ta, a)
        text_b = _evidence_text(db, tb, b)
        if use_model:
            res = classify_relation(question_text, text_a, text_b, model=model)
        else:
            res = {"relation": "not_comparable", "justification": "model disabled",
                   "evidence_references": [], "question_reference": question_text}
        db.link_evidence_relation(
            question_id, ta, a, tb, b, res["relation"],
            justification=res.get("justification"), model=model)
        written += 1
    return written
