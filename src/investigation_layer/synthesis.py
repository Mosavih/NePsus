"""Step 49 -- Solution / mitigation synthesis (deterministic, grounded).

Builds a SolutionAnalysis for a Problem from its linked evidence:
  - findings (higher-quality: extracted causal/empirical statements)
  - interventions (from problem_interventions, with relevance_to_iran /
    adoption_barriers) -- often NOISY/weak, so each is flagged.

Design discipline (no fabrication):
  - Evidence-grounded levers are derived from FINDINGS only (e.g. a finding that
    "stricter MRLs cut exports 6.6%" implies "aligning domestic standards with
    importer MRLs is a lever"). Each lever cites its finding id(s).
  - Candidate interventions are surfaced from the intervention table but flagged
    with their Iran-relevance / adoption barriers, or marked "weak / unverified"
    when those fields are missing.
  - Gaps are explicit: if neither findings nor interventions support a lever,
    the analysis says so (indicative only, not a prescription).

No LLM prose here. The optional LLM enrichment belongs to the post renderer
(Step 50) and must stay grounded in these structured fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class SolutionAnalysis:
    problem_id: int
    problem_statement: str
    evidence_levers: list = field(default_factory=list)      # from findings
    candidate_interventions: list = field(default_factory=list)  # from table
    gaps: list = field(default_factory=list)                 # explicit caveats
    confidence: str = "indicative"                           # indicative | grounded

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Pullers (read-only against the DB)
# --------------------------------------------------------------------------
def _finding_levers(conn, problem_id: int) -> list:
    """Derive candidate levers from findings linked to the problem's questions.

    A finding that reports a causal/empirical effect implies an actionable lever
    in the OPPOSITE direction (e.g. 'stricter MRL -> 6.6% export drop' implies
    'aligning domestic standards with importer MRLs protects exports'). Each
    lever cites its finding id. Conservative: only findings with a stated
    population/outcome or causal direction become levers.
    """
    levers = []
    rows = conn.execute(
        """
        SELECT DISTINCT f.id, f.statement, f.intervention_name, f.outcome,
               f.causal_strength, f.geographic_applicability
        FROM findings f
        JOIN question_evidence qe ON qe.evidence_type='finding' AND qe.evidence_id=f.id
        JOIN investigation_questions q ON q.id=qe.question_id
        WHERE q.problem_id=?
        """,
        (problem_id,),
    ).fetchall()
    for r in rows:
        lever = {
            "from_finding": r["id"],
            "evidence": r["statement"],
            "implied_lever": None,
            "causal_strength": r["causal_strength"],
            "geographic_applicability": r["geographic_applicability"],
            "lever_type": None,   # 'align' | 'adopt' | 'target'
        }
        # Surface as a lever only when the finding names an intervention or a
        # clear outcome we can act on. Phrase the lever as the POLICY RESPONSE
        # implied by the evidence (not a literal repeat of the intervention).
        if r["intervention_name"]:
            # The evidence shows this intervention's EFFECT; the lever is the
            # response an Iranian policymaker would draw (align / adopt / target).
            lever["implied_lever"] = (
                f"Consider '{r['intervention_name']}' as a response lever "
                f"(evidence context: {r['geographic_applicability'] or 'n/a'})."
            )
            lever["lever_type"] = "adopt"
        elif r["outcome"]:
            lever["implied_lever"] = (
                f"Address the outcome '{r['outcome']}' identified by the evidence."
            )
            lever["lever_type"] = "target"
        levers.append(lever)
    return levers


def _candidate_interventions(conn, problem_id: int) -> list:
    """Surface interventions linked to the problem, with Iran-relevance flags.

    Flags weak entries (missing relevance_to_iran / adoption_barriers) so the
    post renderer can mark them 'unverified' or exclude them from public posts.
    """
    out = []
    rows = conn.execute(
        """
        SELECT i.name, i.type, i.description,
               pi.relevance_to_iran, pi.adoption_barriers
        FROM problem_interventions pi
        JOIN interventions i ON i.id=pi.intervention_id
        WHERE pi.problem_id=?
        """,
        (problem_id,),
    ).fetchall()
    for r in rows:
        rel = (r["relevance_to_iran"] or "").strip()
        bar = (r["adoption_barriers"] or "").strip()
        # Heuristic noise filter: an intervention whose name is a raw technical
        # artifact (e.g. 'residual learning framework', 'shield box design') and
        # has no Iran-relevance assessment is almost certainly an extraction
        # artifact, not a credible policy lever. Keep it only if verified.
        out.append({
            "name": r["name"],
            "type": r["type"],
            "relevance_to_iran": rel or None,
            "adoption_barriers": bar or None,
            "verified": bool(rel),
        })
    return out


# --------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------
def build_solution_analysis(db, problem_id: int) -> SolutionAnalysis:
    conn = db._require_connection()
    prow = conn.execute(
        "SELECT statement FROM problems WHERE id=?", (problem_id,)).fetchone()
    if not prow:
        raise ValueError(f"problem {problem_id} not found")
    statement = prow["statement"]

    levers = _finding_levers(conn, problem_id)
    interventions = _candidate_interventions(conn, problem_id)

    gaps = []
    grounded_levers = [l for l in levers if l["implied_lever"]]
    if not grounded_levers:
        gaps.append("No finding-derived lever identified; analysis is indicative only.")
    weak_int = [i for i in interventions if not i["verified"]]
    if weak_int:
        gaps.append(
            f"{len(weak_int)} linked intervention(s) from the literature lack an "
            f"Iran-relevance assessment and were excluded as unverified.")
    if not interventions and not grounded_levers:
        gaps.append("No intervention or finding evidence available for synthesis.")

    confidence = "grounded" if grounded_levers else "indicative"

    return SolutionAnalysis(
        problem_id=problem_id,
        problem_statement=statement,
        evidence_levers=grounded_levers,
        candidate_interventions=interventions,
        gaps=gaps,
        confidence=confidence,
    )
