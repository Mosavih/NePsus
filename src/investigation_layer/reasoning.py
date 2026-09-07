"""Step 21 — Question Reasoning Prototype (smallest reasoning layer).

Builds a STRUCTURED reasoning state from a Question's persisted evidence graph
(question_evidence -> Measurement/Finding + evidence_relation edges). This is an
in-memory object ONLY -- no new database table, no LLM prose, no forecasting, no
autonomous agent.

The reasoning layer does NOT re-derive relationships: Step 19 already computed the
conflict / agree / address_different_aspect / not_comparable edges; this layer
CONSUMES them. It adds:
  - direct_evidence: the raw, un-inferred statements/observations;
  - deterministic_patterns: values computable with NO semantic interpretation
    (latest, abs/percent change, direction, monotonicity, supporting/conflicting
    counts, substrate coverage, co/non-co-movement);
  - evidence_relationships: the actual Step-19 edges (verbatim relation + reason);
  - supported_inferences: only deterministic/derived claims (never semantic leaps);
  - unresolved: explicit "the evidence does not establish a conclusion" outputs,
    especially where conflict edges have no winner;
  - unsupported_claims: structural guards for forbidden leaps the architecture must
    never produce (causal-direction reversal; cross-context applicability -- Step 12).

No model writes the final answer here. The output is the JSON-like dict; deciding
whether to render it to natural language is a separate, later decision.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ReasoningState:
    question: str
    question_id: int
    direct_evidence: list = field(default_factory=list)
    deterministic_patterns: list = field(default_factory=list)
    evidence_relationships: list = field(default_factory=list)
    supported_inferences: list = field(default_factory=list)
    unresolved: list = field(default_factory=list)
    unsupported_claims: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Internal helpers (deterministic, no LLM)
# ---------------------------------------------------------------------------
def _measurement_series(conn, mids):
    """Return [(reference_period, value, indicator, source)] sorted by period."""
    out = []
    for mid in mids:
        r = conn.execute(
            "SELECT id, reference_period, value, indicator, measurement_source, "
            "subject_entity_name, unit, quality, acquisition_method "
            "FROM measurements WHERE id=?",
            (mid,)).fetchone()
        if r:
            out.append((r["id"], r["reference_period"], r["value"], r["indicator"],
                        r["measurement_source"], r["subject_entity_name"],
                        r["unit"], r["quality"], r["acquisition_method"]))
    out.sort(key=lambda x: (x[1] or ""))
    return out


def _measurement_text(r):
    val = r[2]
    try:
        val = f"{float(val):,.2f}"
    except (TypeError, ValueError):
        pass
    unit = r[6] or ""
    return f"{r[3]} | {r[5] or ''} | {r[1]}: {val} {unit}"


def _finding_record(conn, fid):
    # Join to studies so the receiving model can trace a finding back to its
    # source (provenance must survive graph -> ReasoningState; a conflict cannot
    # be weighted for credibility/risk-of-bias without the study identity).
    return conn.execute(
        "SELECT f.statement, f.population, f.outcome, f.intervention_name, "
        "f.causal_strength, f.geographic_applicability, f.limitations, "
        "f.study_id, s.source_work_id, s.year AS study_year, s.study_type, "
        "s.cited_by_count "
        "FROM findings f LEFT JOIN studies s ON s.id = f.study_id WHERE f.id=?",
        (fid,)).fetchone()


def _series_patterns(indicator, series):
    """Deterministic patterns from a single-indicator measurement series.

    series items are (id, reference_period, value, ...)."""
    pats = []
    vals = [(p, v) for (_, p, v, *_) in series if v is not None]
    if len(vals) >= 1:
        last_p, last_v = vals[-1]
        pats.append(f"latest observation ({last_p}): {last_v:,.0f}")
    if len(vals) >= 2:
        first_p, first_v = vals[0]
        last_p, last_v = vals[-1]
        abs_chg = last_v - first_v
        pct = (abs_chg / first_v * 100.0) if first_v else None
        pats.append(
            f"absolute change {first_p}->{last_p}: {abs_chg:,.0f}")
        if pct is not None:
            pats.append(
                f"percentage change {first_p}->{last_p}: {pct:+.1f}%")
        direction = "increasing" if last_v > first_v else (
            "decreasing" if last_v < first_v else "flat")
        pats.append(f"direction over period: {direction}")
        mono = all(vals[i + 1][1] >= vals[i][1] for i in range(len(vals) - 1)) or \
               all(vals[i + 1][1] <= vals[i][1] for i in range(len(vals) - 1))
        pats.append(
            f"monotonic over period: {mono} ("
            f"{'non-decreasing' if vals[-1][1] >= vals[0][1] else 'non-increasing'})")
    return pats


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------
def build_reasoning_state(db, question_id: int) -> ReasoningState:
    conn = db._require_connection()
    qrow = conn.execute(
        "SELECT question, status FROM investigation_questions WHERE id=?",
        (question_id,)).fetchone()
    if not qrow:
        raise ValueError(f"question {question_id} not found")
    question_text = qrow["question"]
    status = qrow["status"]

    state = ReasoningState(question=question_text, question_id=question_id)

    linked = db.get_question_evidence(question_id)
    mids, fids = [], []
    for ev in linked:
        if ev["evidence_type"] == "measurement":
            mids.append(ev["evidence_id"])
        else:
            fids.append(ev["evidence_id"])

    # --- direct_evidence (raw, no inference) ---
    series = _measurement_series(conn, mids)
    for r in series:
        state.direct_evidence.append({
            "type": "measurement",
            "evidence_id": r[0],
            "statement": _measurement_text(r),
            "indicator": r[3],
            "subject": r[5],
            "reference_period": r[1],
            "value": r[2],
            "unit": r[6],
            "measurement_source": r[4],
            "quality": r[7],
            "acquisition_method": r[8],
        })
    for fid in fids:
        rec = _finding_record(conn, fid)
        if rec:
            state.direct_evidence.append({
                "type": "finding",
                "evidence_id": fid,
                "study_id": rec["study_id"],
                "source_work_id": rec["source_work_id"],
                "study_year": rec["study_year"],
                "study_type": rec["study_type"],
                "cited_by_count": rec["cited_by_count"],
                "statement": rec["statement"],
                "intervention": rec["intervention_name"],
                "outcome": rec["outcome"],
                "causal_strength": rec["causal_strength"],
                "geographic_applicability": rec["geographic_applicability"],
                "limitations": rec["limitations"],
            })

    # --- deterministic_patterns ---
    # Group measurements by indicator for series patterns. (series tuple is
    # (id, reference_period, value, indicator, source, subject, unit, quality, acq),
    # so the indicator key is r[3].)
    by_ind = {}
    for r in series:
        by_ind.setdefault(r[3], []).append(r)
    for ind, ser in by_ind.items():
        state.deterministic_patterns.extend(
            f"{ind}: {p}" for p in _series_patterns(ind, ser))

    # Substrate coverage + study/finding counts.
    state.deterministic_patterns.append(
        f"substrate coverage: {len(mids)} measurement(s), {len(fids)} finding(s)")

    # Evidence relationships (Step 19 edges) -> counts + consume verbatim.
    relations = db.get_evidence_relations(question_id)
    n_conflict = n_agree = n_diff = n_nc = 0

    def _study_of(etype, eid):
        if etype == "finding":
            s = conn.execute(
                "SELECT study_id FROM findings WHERE id=?", (eid,)).fetchone()
            return s["study_id"] if s else None
        return None

    for rel in relations:
        rtype = rel["relation"]
        if rtype == "conflict":
            n_conflict += 1
        elif rtype == "agree":
            n_agree += 1
        elif rtype == "address_different_aspect":
            n_diff += 1
        else:
            n_nc += 1
        a_type, a_id = rel["evidence_a_type"], rel["evidence_a_id"]
        b_type, b_id = rel["evidence_b_type"], rel["evidence_b_id"]
        entry = {
            "relation": rtype,
            "a": f"{a_type}:{a_id}",
            "b": f"{b_type}:{b_id}",
            "a_study_id": _study_of(a_type, a_id),
            "b_study_id": _study_of(b_type, b_id),
            "justification": rel["justification"],
        }
        state.evidence_relationships.append(entry)
    # Count distinct supporting studies for findings.
    study_ids = set()
    for fid in fids:
        s = conn.execute(
            "SELECT study_id FROM findings WHERE id=?", (fid,)).fetchone()
        if s and s["study_id"]:
            study_ids.add(s["study_id"])
    state.deterministic_patterns.append(
        f"supporting studies: {len(study_ids)}")
    state.deterministic_patterns.append(
        f"conflicting finding pairs: {n_conflict}")
    state.deterministic_patterns.append(
        f"agreeing finding pairs: {n_agree}")

    # --- supported_inferences (deterministic only) ---
    # Only restatements of computed patterns, never semantic leaps. Each carries
    # evidence_refs so the receiving model can trace the inference back to the
    # exact measurement/finding IDs it summarizes.
    for ind, ser in by_ind.items():
        ids = [r[0] for r in ser]
        vals = [(p, v) for (_, p, v, *_) in ser if v is not None]
        if len(vals) >= 2:
            fp, fv = vals[0]
            lp, lv = vals[-1]
            state.supported_inferences.append({
                "claim": f"{ind} moved from {fv:,.0f} ({fp}) to {lv:,.0f} ({lp}); "
                         f"the direction and magnitude are read directly from the series.",
                "evidence_refs": [f"measurement:{i}" for i in ids],
            })
    if n_conflict:
        conflict_edges = [
            f"{e['a']} --{e['relation']}-- {e['b']}"
            for e in state.evidence_relationships if e["relation"] == "conflict"]
        state.supported_inferences.append({
            "claim": "Conflicting findings are present; their existence is directly "
                     "established by the evidence edges.",
            "evidence_refs": conflict_edges,
        })

    # --- unresolved ---
    if status in ("no_evidence",):
        state.unresolved.append({
            "reason": "no relevant evidence was retrieved for this Question",
            "resolution": "unresolved",
        })
    if status in ("deferred",):
        state.unresolved.append({
            "reason": "operational/source failure during retrieval "
                      "(not an evidence-coverage judgment)",
            "resolution": "unresolved -- recoverable",
        })
    for rel in relations:
        if rel["relation"] == "conflict":
            state.unresolved.append({
                "reason": "conflicting findings; no evidence relation "
                          "establishes a winner",
                "resolution": "unresolved",
                "edge": f"{rel['evidence_a_type']}:{rel['evidence_a_id']} "
                        f"--conflict-- {rel['evidence_b_type']}:{rel['evidence_b_id']}",
            })
        elif rel["relation"] == "not_comparable":
            state.unresolved.append({
                "reason": "the evidence does not contain enough semantic "
                          "information to relate these items",
                "resolution": "relationship undetermined",
                "edge": f"{rel['evidence_a_type']}:{rel['evidence_a_id']} "
                        f"--not_comparable-- {rel['evidence_b_type']}:{rel['evidence_b_id']}",
            })

    # --- unsupported_claims (structural guards; never LLM-written) ---
    # Guard 1: opposite directions between two measurements != causation.
    for rel in relations:
        if rel["relation"] == "address_different_aspect":
            a_type, a_id = rel["evidence_a_type"], rel["evidence_a_id"]
            b_type, b_id = rel["evidence_b_type"], rel["evidence_b_id"]
            if a_type == "measurement" and b_type == "measurement":
                av = conn.execute(
                    "SELECT value, reference_period FROM measurements WHERE id=?",
                    (a_id,)).fetchone()
                bv = conn.execute(
                    "SELECT value, reference_period FROM measurements WHERE id=?",
                    (b_id,)).fetchone()
                if av and bv and av["value"] is not None and bv["value"] is not None:
                    # Determine direction of change over each series' own span. A
                    # robust guard: if the two measurements moved in OPPOSITE
                    # directions (one rising, one falling), that does NOT imply one
                    # caused the other. We compare each measure's first vs last
                    # available value rather than value sign (both are positive $).
                    def _first_last(mid):
                        rows = conn.execute(
                            "SELECT value FROM measurements WHERE id=? ORDER BY "
                            "reference_period", (mid,)).fetchall()
                        vals = [r["value"] for r in rows if r["value"] is not None]
                        if len(vals) < 2:
                            # fall back to the single linked value
                            return (vals[0] if vals else None, vals[0] if vals else None)
                        return (vals[0], vals[-1])
                    a0, a1 = _first_last(a_id)
                    b0, b1 = _first_last(b_id)
                    if None not in (a0, a1, b0, b1):
                        a_dir = (a1 > a0) - (a1 < a0)   # +1 / -1 / 0
                        b_dir = (b1 > b0) - (b1 < b0)
                        if a_dir != 0 and b_dir != 0 and a_dir != b_dir:
                            state.unsupported_claims.append(
                                "the two measurements moved in opposite directions; "
                                "this does NOT imply that one caused the other -- no "
                                "causal claim is supported by the relationship edge.")
    # Guard 2: cross-context applicability (Step 12). A finding whose
    # geographic_applicability is not established for the Question's subject must
    # not be taken to imply applicability to that subject.
    q_subj = conn.execute(
        "SELECT e.name FROM investigation_questions q "
        "JOIN problem_entities pe ON pe.problem_id=q.problem_id "
        "JOIN entities e ON e.id=pe.entity_id WHERE q.id=? LIMIT 1",
        (question_id,)).fetchone()
    q_subject = (q_subj["name"] if q_subj else None)
    for fid in fids:
        rec = _finding_record(conn, fid)
        ga = (rec["geographic_applicability"] or "").strip() if rec else ""
        if ga and q_subject and ga.lower() != q_subject.lower():
            state.unsupported_claims.append(
                f"finding ({fid}) is scoped to '{ga}', not to '{q_subject}'; "
                "do NOT infer the intervention/outcome is applicable to "
                f"'{q_subject}' (applicability is separate from effectiveness -- Step 12).")
        # When ga matches q_subject, applicability to the subject IS established by
        # the finding's own scope; we do NOT emit a (false) "beyond scope" warning.
    return state
