"""Step 44 -- Human-readable brief renderer (the "think tank" output).

Consumes the structured ReasoningState (build_reasoning_state, Step 21) and
renders it to GROUNDED Markdown -- no LLM prose, no semantic leaps. Every line
is a faithful restatement of a structured field (direct_evidence,
deterministic_patterns, evidence_relationships, supported_inferences, unresolved,
unsupported_claims). Where a Question has no linked evidence, the brief says
EVIDENCE PENDING -- it never invents a conclusion.

This is the layer that turns Investigation output into a readable per-Problem
brief: one section per Investigation Question, evidence cited by id, conflicts
and gaps made explicit.
"""
from __future__ import annotations

from src.investigation_layer.reasoning import build_reasoning_state, ReasoningState


def _esc(s: str) -> str:
    return (s or "").strip()


def render_question_brief(state: ReasoningState) -> str:
    d = state.to_dict()
    lines = []
    lines.append(f"### Q{state.question_id}: {_esc(state.question)}")
    lines.append("")

    ev = d.get("direct_evidence", [])
    if not ev:
        lines.append("**EVIDENCE PENDING** — no measurements or findings are "
                     "linked to this question yet.")
        # Surface any unresolved status so the gap is explicit.
        for u in d.get("unresolved", []):
            lines.append(f"- _unresolved_: {_esc(u.get('reason', ''))}")
        lines.append("")
        return "\n".join(lines)

    # Direct evidence (measurements + findings), cited by id.
    lines.append("**Direct evidence**")
    for e in ev:
        if e.get("type") == "measurement":
            lines.append(f"- [measurement:{e['evidence_id']}] {_esc(e.get('statement'))} "
                         f"_(source: {_esc(e.get('measurement_source'))}, "
                         f"quality: {_esc(str(e.get('quality')))})_")
        else:
            geo = e.get("geographic_applicability")
            cs = e.get("causal_strength")
            tail = ""
            if geo:
                tail += f" geo={_esc(geo)}"
            if cs:
                tail += f" strength={_esc(cs)}"
            lines.append(f"- [finding:{e['evidence_id']}] {_esc(e.get('statement'))} "
                         f"_(study {_esc(str(e.get('study_id')))}, "
                         f"year {_esc(str(e.get('study_year')))}, "
                         f"cited_by={_esc(str(e.get('cited_by_count')))}{tail})_")
    lines.append("")

    # Deterministic patterns (e.g. series direction/magnitude, coverage counts).
    pats = d.get("deterministic_patterns", [])
    if pats:
        lines.append("**Deterministic patterns**")
        for p in pats:
            lines.append(f"- {_esc(p)}")
        lines.append("")

    # Evidence relationships (Step 19 edges) -- conflicts/agreements explicit.
    rels = d.get("evidence_relationships", [])
    if rels:
        lines.append("**Evidence relationships**")
        for r in rels:
            lines.append(f"- {_esc(r.get('a'))} --{_esc(r.get('relation'))}-- "
                         f"{_esc(r.get('b'))}: {_esc(r.get('justification'))}")
        lines.append("")

    # Supported inferences (deterministic restatements, with evidence_refs).
    infs = d.get("supported_inferences", [])
    if infs:
        lines.append("**Supported inferences**")
        for i in infs:
            refs = ", ".join(i.get("evidence_refs", []))
            lines.append(f"- {_esc(i.get('claim'))} _(refs: {_esc(refs)})_")
        lines.append("")

    # Unresolved + unsupported guards -- make gaps explicit (no false closure).
    unres = d.get("unresolved", [])
    if unres:
        lines.append("**Unresolved / gaps**")
        for u in unres:
            lines.append(f"- {_esc(u.get('reason', ''))}"
                         f"{(' edge=' + _esc(u['edge'])) if u.get('edge') else ''}")
        lines.append("")

    return "\n".join(lines)


def render_problem_brief(db, problem_id: int) -> str:
    """Render a full per-Problem brief: problem statement + one section per
    Investigation Question (evidence-grounded or EVIDENCE PENDING)."""
    conn = db._require_connection()
    prow = conn.execute(
        "SELECT statement FROM problems WHERE id=?", (problem_id,)).fetchone()
    if not prow:
        return f"_problem {problem_id} not found_"
    stmt = prow["statement"]
    qrows = conn.execute(
        "SELECT id, question FROM investigation_questions WHERE problem_id=? "
        "ORDER BY rank", (problem_id,)).fetchall()

    lines = []
    lines.append(f"# Problem {problem_id} Brief")
    lines.append("")
    lines.append(f"**Problem statement:** {_esc(stmt)}")
    lines.append("")
    lines.append(f"**Investigation Questions:** {len(qrows)}")
    lines.append("")
    for q in qrows:
        state = build_reasoning_state(db, q["id"])
        lines.append(render_question_brief(state))
        lines.append("")
    return "\n".join(lines)
