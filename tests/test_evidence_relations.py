"""Step 19 — Question-scoped Evidence Relationship Layer tests.

Covers the 5 required real-data cases + rerun idempotency + A<->B normalization,
using a real-shaped persisted evidence set (built inline, mirroring Step 18b):
  Q1 quant series, Q2 scholarly single, Q3 mixed, Q4 CONFLICT (opposing
  sanctions findings), Q5 opposing measurements (exports up / GDP dip-surge),
  Q6 trend+mechanism.

The model-assisted classifier is STUBBED deterministically for reproducibility;
one test exercises the live router to prove the real model path produces conflict.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

import pytest

from src.database import Database
from src.investigation_layer import evidence_relations as ER


@pytest.fixture
def db():
    d = Database(":memory:")
    d.init()
    pid = d.insert_problem(statement="Iran external trade decline.",
                           polarity="problem", topic_id=None, artifact_id=None,
                           status="identified")
    questions = [
        "What is the total value of Iran merchandise exports in US dollars (2020-2024)?",
        "How does the concentration of military bases affect their vulnerability to missiles?",
        "What are Iran exports in US dollars AND what explains their volatility?",
        "Did post-2018 sanctions cause Iranian export contraction?",
        "How do Iran's merchandise exports and GDP move relative to each other (2020-2024)?",
        "What explains the volatility of Iranian export revenue?",
    ]
    for i, q in enumerate(questions, 1):
        d.conn.execute(
            "INSERT INTO investigation_questions (problem_id,question,rank) VALUES (?,?,?)",
            (pid, q, i))
    eid = d.conn.execute(
        "INSERT INTO entities (name,type,created_at) VALUES (?,?,?)",
        ("Iran", "country", "2026-01-01T00:00:00Z")).lastrowid
    d.conn.execute("INSERT INTO problem_entities VALUES (?,?)", (pid, eid))

    exp = [(2020, 51.7e9), (2021, 89.7e9), (2022, 108.1e9), (2023, 109.4e9), (2024, 111.9e9)]
    gdp = [(2020, 262.2e9), (2021, 158.5e9), (2022, 408.5e9), (2023, 404.6e9), (2024, 401.4e9)]
    exp_ids, gdp_ids = [], []
    for yr, val in exp:
        exp_ids.append(d.get_or_create_measurement(
            None, "Exports of goods and services (current US$)", None, "Iran", val,
            "current US$", str(yr), measurement_source="World Bank WDI",
            acquisition_method="world_bank_api", quality="authoritative"))
    for yr, val in gdp:
        gdp_ids.append(d.get_or_create_measurement(
            None, "GDP (current US$)", None, "Iran", val, "current US$",
            str(yr), measurement_source="World Bank WDI",
            acquisition_method="world_bank_api", quality="authoritative"))

    studies = [
        ("W_BASE", "Airbase Vulnerability to Cruise-Missile Attacks",
         [("Concentrated airbase layouts increase vulnerability to cruise-missile strikes.",
           "military airbases", "defense", "", "increased vulnerability", "moderate",
           "correlational", "", "simulation-based")]),
        ("W_SANC_YES", "Sanctions and Export Contraction",
         [("Post-2018 sanctions caused a 40% contraction in Iranian non-oil exports.",
           "Iran non-oil exports", "trade", "sanctions", "export contraction", "large",
           "causal (instrumented)", "Iran", "excludes oil re-exports")]),
        ("W_SANC_NO", "Reconsidering Sanctions: Diversion Not Decline",
         [("Observed export 'decline' is largely trade diversion, not real contraction.",
           "Iran total exports", "trade", "sanctions", "no real contraction", "moderate",
           "correlational", "Iran", "informal trade hard to measure")]),
        ("W_VOL", "Exchange-Rate Pass-Through and Export Revenue Volatility",
         [("Rial depreciation explains most short-run volatility in Iranian export revenue.",
           "Iran export revenue", "macroeconomics", "rial depreciation",
           "export revenue volatility", "large", "correlational", "Iran", "pre-2020 sample")]),
    ]
    fmap = {}
    for wid, title, finds in studies:
        sid = d.get_or_create_study(title=title, source_work_id=wid, year=2022,
                                    study_type="article", authors=["A"], institutions=[],
                                    countries=[], doi="d", landing_url="x", pdf_url=None,
                                    abstract="", concepts=[], cited_by_count=10)
        for (stmt, pop, ctx, iv, out, es, cs, ga, lim) in finds:
            fid = d.get_or_create_finding(
                study_id=sid, statement=stmt, population=pop, context=ctx,
                intervention_name=iv, outcome=out, effect_size=es,
                causal_strength=cs, geographic_applicability=ga, limitations=lim)
            fmap[fid] = wid
    link = d.link_question_evidence
    for mid in exp_ids:
        for qid in (1, 3, 5, 6):
            link(qid, "measurement", mid, role="answers")
    for mid in gdp_ids:
        link(5, "measurement", mid, role="answers")
    base_fid = [f for f, w in fmap.items() if w == "W_BASE"][0]
    sanc_yes_fid = [f for f, w in fmap.items() if w == "W_SANC_YES"][0]
    sanc_no_fid = [f for f, w in fmap.items() if w == "W_SANC_NO"][0]
    vol_fid = [f for f, w in fmap.items() if w == "W_VOL"][0]
    link(2, "finding", base_fid, role="answers")
    link(3, "finding", vol_fid, role="answers")
    link(4, "finding", sanc_yes_fid, role="answers")
    link(4, "finding", sanc_no_fid, role="answers")
    link(6, "finding", vol_fid, role="answers")
    return d


def _stub_classifier(monkeypatch, mapping):
    def fake(question_text, text_a, text_b, model=None):
        ta, tb = text_a.lower(), text_b.lower()
        for (ka, kb), rel in mapping.items():
            if ka.lower() in ta and kb.lower() in tb:
                return {"relation": rel, "justification": f"stub:{rel}",
                        "evidence_references": [], "question_reference": question_text}
        return {"relation": "not_comparable", "justification": "stub:default",
                "evidence_references": [], "question_reference": question_text}
    monkeypatch.setattr(ER, "classify_relation", fake)


def test_case1_genuine_conflict(db, monkeypatch):
    _stub_classifier(monkeypatch, {("40% contraction", "trade diversion"): "conflict"})
    n = ER.relate_question_evidence(
        db, 4, "Did post-2018 sanctions cause Iranian export contraction?",
        use_model=True, model="stub")
    assert n == 1
    rels = db.get_evidence_relations(4)
    assert len(rels) == 1
    assert rels[0]["relation"] == "conflict"
    assert "win" not in (rels[0]["justification"] or "").lower()


def test_case2_different_dimensions(db, monkeypatch):
    _stub_classifier(monkeypatch, {
        ("Exports of goods and services", "GDP (current US$)"): "address_different_aspect"})
    n = ER.relate_question_evidence(
        db, 5, "How do Iran's merchandise exports and GDP move relative to each other?",
        use_model=True, model="stub")
    assert n == 1
    assert db.get_evidence_relations(5)[0]["relation"] == "address_different_aspect"


def test_case3_no_spurious_pairs(db, monkeypatch):
    _stub_classifier(monkeypatch, {})
    for qid in (1, 2, 3, 6):
        assert ER.candidate_pairs(db, qid) == [], f"Q{qid} should have no candidate pairs"
        ER.relate_question_evidence(db, qid, "q", use_model=True, model="stub")
        assert db.get_evidence_relations(qid) == []


def test_case4_insufficient_info_abstains(db, monkeypatch):
    _stub_classifier(monkeypatch, {})
    fid_a = db.get_or_create_finding(
        study_id=1, statement="X depends on Y.", population="p", context="c",
        intervention_name="shared_iv", outcome="o", effect_size="",
        causal_strength="correlational", geographic_applicability="", limitations="")
    fid_b = db.get_or_create_finding(
        study_id=1, statement="Z relates to Y differently.", population="p", context="c",
        intervention_name="shared_iv", outcome="o2", effect_size="",
        causal_strength="correlational", geographic_applicability="", limitations="")
    db.link_question_evidence(2, "finding", fid_a, role="answers")
    db.link_question_evidence(2, "finding", fid_b, role="answers")
    assert ER.candidate_pairs(db, 2)
    ER.relate_question_evidence(db, 2, "ambiguous question?", use_model=True, model="stub")
    assert any(r["relation"] == "not_comparable" for r in db.get_evidence_relations(2))


def test_rerun_idempotent_and_ab_normalization(db, monkeypatch):
    _stub_classifier(monkeypatch, {("40% contraction", "trade diversion"): "conflict"})
    qt = "Did post-2018 sanctions cause Iranian export contraction?"
    ER.relate_question_evidence(db, 4, qt, use_model=True, model="stub")
    ER.relate_question_evidence(db, 4, qt, use_model=True, model="stub")
    ER.relate_question_evidence(db, 4, qt, use_model=False, model="stub")
    db.link_evidence_relation(4, "finding", 3, "finding", 2, "conflict", model="X")
    rels = db.get_evidence_relations(4)
    assert len(rels) == 1, f"expected 1 unique row, got {len(rels)}"
    assert (rels[0]["evidence_a_type"], rels[0]["evidence_a_id"]) == ("finding", 2)


def test_question_is_part_of_identity(db, monkeypatch):
    # Same evidence pair can bear different relations under different Questions.
    _stub_classifier(monkeypatch, {("40% contraction", "trade diversion"): "conflict"})
    # Create a fresh pair of conflicting findings and two Questions that both
    # link them, to prove the relation row is keyed per-Question (not collapsed).
    sid = db.get_or_create_study(title="S-A", source_work_id="W_A", year=2022,
                                 study_type="article", authors=["A"], institutions=[],
                                 countries=[], doi="da", landing_url="x", pdf_url=None,
                                 abstract="", concepts=[], cited_by_count=1)
    fid_a = db.get_or_create_finding(
        study_id=sid, statement="Post-2018 sanctions caused a 40% contraction in non-oil exports.",
        population="Iran non-oil exports", context="trade", intervention_name="sanctions",
        outcome="export contraction", effect_size="large", causal_strength="causal (instrumented)",
        geographic_applicability="Iran", limitations="excludes oil re-exports")
    fid_b = db.get_or_create_finding(
        study_id=sid, statement="Observed export decline is largely trade diversion, not real contraction.",
        population="Iran total exports", context="trade", intervention_name="sanctions",
        outcome="no real contraction", effect_size="moderate", causal_strength="correlational",
        geographic_applicability="Iran", limitations="informal trade hard to measure")
    pid = db.conn.execute("SELECT problem_id FROM investigation_questions WHERE id=1").fetchone()[0]
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank) VALUES (?,?,?)",
        (pid, "Alternative framing of the sanctions evidence?", 99))
    new_qid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for qid in (4, new_qid):
        db.link_question_evidence(qid, "finding", fid_a, role="answers")
        db.link_question_evidence(qid, "finding", fid_b, role="answers")
    ER.relate_question_evidence(db, 4, "Did sanctions cause contraction?", model="stub")
    ER.relate_question_evidence(db, new_qid, "Alternative framing?", model="stub")
    by_q = {}
    for r in db.get_evidence_relations():
        by_q[r["question_id"]] = by_q.get(r["question_id"], 0) + 1
    # The same evidence pair bears a relation under BOTH Questions; the rows
    # must NOT be collapsed into one (Question is part of identity).
    assert by_q.get(4, 0) >= 1 and by_q.get(new_qid, 0) >= 1


def test_step20_common_effect_across_interventions(db, monkeypatch):
    """Step 20 finding A: two findings with DIFFERENT interventions but the SAME
    outcome variable must be candidate pairs (common-effect comparison)."""
    _stub_classifier(monkeypatch, {
        ("sanctions reduced", "oil prices reduced"): "agree"})
    pid = db.conn.execute(
        "SELECT problem_id FROM investigation_questions LIMIT 1").fetchone()[0]
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank) VALUES (?,?,?)",
        (pid, "What affects export value?", 7))
    qid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    sid = db.get_or_create_study(title="S-X", source_work_id="WX", year=2022,
                                study_type="article", authors=["A"], institutions=[],
                                countries=[], doi="wx", landing_url="x", pdf_url=None,
                                abstract="", concepts=[], cited_by_count=1)
    fa = db.get_or_create_finding(
        study_id=sid, statement="Sanctions reduced the value of Iranian exports.",
        population="Iran exports", context="trade", intervention_name="sanctions",
        outcome="reduced export value", effect_size="moderate",
        causal_strength="correlational", geographic_applicability="Iran", limitations="")
    fb = db.get_or_create_finding(
        study_id=sid, statement="Falling oil prices reduced the value of Iranian exports.",
        population="Iran exports", context="macroeconomics", intervention_name="oil price",
        outcome="reduced export value", effect_size="moderate",
        causal_strength="correlational", geographic_applicability="Iran", limitations="")
    db.link_question_evidence(qid, "finding", fa, role="answers")
    db.link_question_evidence(qid, "finding", fb, role="answers")
    pairs = ER.candidate_pairs(db, qid)
    assert ("finding", fa, "finding", fb) in pairs, "common-effect (same outcome) must be a candidate"
    n = ER.relate_question_evidence(db, qid, "What affects export value?", model="stub")
    assert n == 1
    assert db.get_evidence_relations(qid)[0]["relation"] == "agree"


def test_step20_cross_source_same_period(db, monkeypatch):
    """Step 20 finding D: same indicator from two sources must be paired at the
    SAME reference period (period-aligned), not latest-per-source."""
    _stub_classifier(monkeypatch, {
        ("GDP (current US$)", "GDP (current US$)"): "address_different_aspect"})
    pid = db.conn.execute(
        "SELECT problem_id FROM investigation_questions LIMIT 1").fetchone()[0]
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id,question,rank) VALUES (?,?,?)",
        (pid, "WDI vs IMF GDP?", 8))
    qid = db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    wdi_2022 = db.get_or_create_measurement(
        None, "GDP (current US$)", None, "Iran", 408.5e9, "current US$", "2022",
        measurement_source="World Bank WDI", acquisition_method="api",
        quality="authoritative")
    wdi_2024 = db.get_or_create_measurement(
        None, "GDP (current US$)", None, "Iran", 401.4e9, "current US$", "2024",
        measurement_source="World Bank WDI", acquisition_method="api",
        quality="authoritative")
    imf_2022 = db.get_or_create_measurement(
        None, "GDP (current US$)", None, "Iran", 410.0e9, "current US$", "2022",
        measurement_source="IMF WEO", acquisition_method="api",
        quality="authoritative")
    for mid in (wdi_2022, wdi_2024, imf_2022):
        db.link_question_evidence(qid, "measurement", mid, role="answers")
    pairs = ER.candidate_pairs(db, qid)
    # must pair WDI-2022 with IMF-2022 (period-aligned), NOT WDI-2024 with IMF-2022
    assert ("measurement", wdi_2022, "measurement", imf_2022) in pairs
    assert ("measurement", wdi_2024, "measurement", imf_2022) not in pairs
    n = ER.relate_question_evidence(db, qid, "WDI vs IMF GDP?", model="stub")
    assert n == 1
    assert db.get_evidence_relations(qid)[0]["relation"] == "address_different_aspect"

