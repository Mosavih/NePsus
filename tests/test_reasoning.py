"""Step 21 — Question Reasoning Prototype tests (deterministic, no LLM).

Builds a small real-shaped DB, runs Step 19 relation extraction, then asserts the
ReasoningState structure is defensible: consumes (not rediscovers) edges, emits
unresolved for conflicts with no winner, and emits the Step-12 cross-context
applicability guard. The classifier is stubbed deterministically.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

import pytest

from src.database import Database
from src.investigation_layer import evidence_relations as ER
from src.investigation_layer import reasoning as RS


def _stub_classify(question_text, text_a, text_b, model=None):
    ta, tb = text_a.lower(), text_b.lower()
    if ("contraction" in ta and "diversion" in tb) or ("diversion" in ta and "contraction" in tb):
        return {"relation": "conflict", "justification": "opposite outcomes",
                "evidence_references": [], "question_reference": question_text}
    if "raised agricultural exports" in ta or "raised agricultural exports" in tb:
        return {"relation": "agree", "justification": "same mechanism",
                "evidence_references": [], "question_reference": question_text}
    return {"relation": "not_comparable", "justification": "default",
            "evidence_references": [], "question_reference": question_text}


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(ER, "classify_relation", _stub_classify)
    d = Database(":memory:")
    d.init()
    pid = d.insert_problem(statement="Iran export decline.", polarity="problem",
                           topic_id=None, artifact_id=None, status="identified")
    eid = d.conn.execute(
        "INSERT INTO entities (name,type,created_at) VALUES (?,?,?)",
        ("Iran", "country", "2026-01-01T00:00:00Z")).lastrowid
    d.conn.execute("INSERT INTO problem_entities VALUES (?,?)", (pid, eid))
    d.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank,status) "
        "VALUES (?,?,?,?)", (pid, "Did sanctions cause contraction?", 1, "answered"))
    d.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank,status) "
        "VALUES (?,?,?,?)", (pid, "What explains export volatility?", 2, "answered"))
    d.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank,status) "
        "VALUES (?,?,?,?)", (pid, "A question with no evidence?", 3, "no_evidence"))
    q1 = d.conn.execute("SELECT id FROM investigation_questions WHERE rank=1").fetchone()[0]
    q2 = d.conn.execute("SELECT id FROM investigation_questions WHERE rank=2").fetchone()[0]
    q3 = d.conn.execute("SELECT id FROM investigation_questions WHERE rank=3").fetchone()[0]

    s1 = d.get_or_create_study(title="A", source_work_id="WA", year=2022, study_type="article",
                              authors=["x"], institutions=[], countries=[], doi="a",
                              landing_url="x", pdf_url=None, abstract="", concepts=[],
                              cited_by_count=1)
    s2 = d.get_or_create_study(title="B", source_work_id="WB", year=2022, study_type="article",
                              authors=["y"], institutions=[], countries=[], doi="b",
                              landing_url="x", pdf_url=None, abstract="", concepts=[],
                              cited_by_count=1)
    fa = d.get_or_create_finding(study_id=s1, statement="Sanctions caused 40% contraction.",
        population="Iran non-oil", context="trade", intervention_name="sanctions",
        outcome="export contraction", effect_size="large", causal_strength="causal (instrumented)",
        geographic_applicability="Iran", limitations="")
    fb = d.get_or_create_finding(study_id=s2, statement="Decline is trade diversion, not contraction.",
        population="Iran total", context="trade", intervention_name="sanctions",
        outcome="no real contraction", effect_size="moderate", causal_strength="correlational",
        geographic_applicability="Iran", limitations="")
    # Senegal-scoped finding linked to q2 (Iran) -> cross-context guard must fire.
    s3 = d.get_or_create_study(title="C", source_work_id="WC", year=2021, study_type="article",
                              authors=["z"], institutions=[], countries=[], doi="c",
                              landing_url="x", pdf_url=None, abstract="", concepts=[],
                              cited_by_count=1)
    fc = d.get_or_create_finding(study_id=s3, statement="Cash transfer raised agricultural exports in Senegal.",
        population="Senegal", context="dev", intervention_name="cash transfer",
        outcome="raised agricultural exports", effect_size="moderate",
        causal_strength="causal (instrumented)", geographic_applicability="Senegal",
        limitations="RCT")
    for q, fid in ((q1, fa), (q1, fb), (q2, fc)):
        d.link_question_evidence(q, "finding", fid, role="answers")
    return d, (q1, q2, q3)


def test_consumes_conflict_edge_and_unresolved(db):
    d, (q1, q2, q3) = db
    qt1 = d.conn.execute("SELECT question FROM investigation_questions WHERE id=?", (q1,)).fetchone()[0]
    ER.relate_question_evidence(d, q1, qt1, use_model=True, model="stub")
    st = RS.build_reasoning_state(d, q1)
    rels = {(r["relation"], r["a"], r["b"]) for r in st.evidence_relationships}
    assert ("conflict", "finding:1", "finding:2") in rels  # Step 19 edge consumed
    assert any(u["resolution"] == "unresolved" and "conflicting findings" in u["reason"]
               for u in st.unresolved)
    # no winner is inferred
    assert not any("winner" in str(x).lower() or "proven" in str(x).lower()
                   for x in st.supported_inferences)


def test_provenance_survives_graph_to_state(db):
    """Step 22: provenance must survive DB -> ReasoningState so a receiving model
    can trace a finding/edge back to its study WITHOUT re-querying the DB."""
    d, (q1, q2, q3) = db
    qt1 = d.conn.execute("SELECT question FROM investigation_questions WHERE id=?", (q1,)).fetchone()[0]
    ER.relate_question_evidence(d, q1, qt1, use_model=True, model="stub")
    st = RS.build_reasoning_state(d, q1)
    # finding carries study identity
    f = next(e for e in st.direct_evidence if e["type"] == "finding")
    assert f["study_id"] is not None
    assert f["source_work_id"] is not None
    # conflict edge carries both study ids
    edge = next(r for r in st.evidence_relationships if r["relation"] == "conflict")
    assert edge["a_study_id"] is not None and edge["b_study_id"] is not None
    # a model can map finding -> study
    assert edge["a"].startswith("finding:")
    fid = int(edge["a"].split(":")[1])
    src = next(e for e in st.direct_evidence if e["type"] == "finding" and e["evidence_id"] == fid)
    assert src["study_id"] == edge["a_study_id"]


def test_supported_inferences_carry_evidence_refs(db):
    """Step 22: every supported_inference must be traceable to evidence IDs."""
    d, (q1, q2, q3) = db
    st = RS.build_reasoning_state(d, q1)
    # no evidence -> no inferences
    assert st.supported_inferences == []
    # on a quantitative question, inferences carry evidence_refs
    qt1 = d.conn.execute("SELECT question FROM investigation_questions WHERE id=?", (q1,)).fetchone()[0]
    ER.relate_question_evidence(d, q1, qt1, use_model=True, model="stub")
    st2 = RS.build_reasoning_state(d, q1)
    assert any("evidence_refs" in inf for inf in st2.supported_inferences)


def test_cross_context_applicability_guard(db):
    d, (q1, q2, q3) = db
    qt2 = d.conn.execute("SELECT question FROM investigation_questions WHERE id=?", (q2,)).fetchone()[0]
    ER.relate_question_evidence(d, q2, qt2, use_model=True, model="stub")
    st = RS.build_reasoning_state(d, q2)
    assert any("Senegal" in c and "Step 12" in c for c in st.unsupported_claims)


def test_no_evidence_unresolved(db):
    d, (q1, q2, q3) = db
    st = RS.build_reasoning_state(d, q3)
    assert st.direct_evidence == []
    assert any(u["resolution"] == "unresolved" for u in st.unresolved)


def test_structure_has_all_required_keys(db):
    d, (q1, q2, q3) = db
    st = RS.build_reasoning_state(d, q1)
    keys = set(st.to_dict().keys())
    assert {"question", "direct_evidence", "deterministic_patterns",
            "evidence_relationships", "supported_inferences", "unresolved",
            "unsupported_claims"} <= keys
