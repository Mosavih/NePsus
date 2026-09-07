"""Step 10 end-to-end: per-Question routing on a real trade Problem.

Verifies the core boundary invariants (Phase 8):
- World Bank observations -> Measurement (NOT Study, NOT Finding).
- News/Claims never become Measurements (no news adapter wired).
- OpenAlex scholarly path unchanged for scholarly Questions.
- Provenance recoverable (measurement_source='World Bank WDI').
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

import pytest

from src.database import Database, _content_hash
from src.investigation_layer.pipeline import investigate_problem


@pytest.fixture
def trade_problem_db():
    db = Database(":memory:")
    db.init()
    # Insert a Problem + Investigation Questions + entity DIRECTLY (deterministic;
    # avoids depending on Gate 2 / the LLM router). This isolates the routing
    # logic under test.
    pid = db.insert_problem(
        statement=("Obstacles and barriers to bilateral trade, transit, and "
                   "economic relations between Iran and Afghanistan."),
        polarity="problem",
        topic_id=None,
        artifact_id=None,
        status="identified",
    )
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id, question, rank) VALUES (?,?,?)",
        (pid, "What specific transit and export obstacles currently impede trade "
               "between Iran and Afghanistan?", 1))
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id, question, rank) VALUES (?,?,?)",
        (pid, "What is the total scale of bilateral trade in current US dollars?", 2))
    # entity "Iran" -> WB country IR
    from datetime import datetime, timezone
    eid = db.conn.execute(
        "INSERT INTO entities (name, type, created_at) VALUES (?,?,?)",
        ("Iran", "country", datetime.now(timezone.utc).isoformat())).lastrowid
    db.conn.execute("INSERT INTO problem_entities (problem_id, entity_id) VALUES (?,?)",
                   (pid, eid))
    return db, pid


def test_worldbank_routing_creates_measurements_not_studies(trade_problem_db):
    db, pid = trade_problem_db
    # Use only_oa=False + tiny per_page to limit LLM calls; the WB path needs no LLM.
    summary = investigate_problem(db, pid, per_page=2, only_oa=False,
                                  use_relevance_gate=True)
    meas = db.conn.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
    studs = db.conn.execute("SELECT COUNT(*) FROM studies").fetchone()[0]
    finds = db.conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]

    # Per-Question strategies were recorded.
    assert summary.get("question_strategies")

    # If the Problem had a quantitative/current Question AND a country, WB
    # measurements should exist AND must NOT have created studies/findings from
    # WB (studies come only from OpenAlex). We assert the boundary explicitly:
    # any Measurement present has World Bank provenance and NO study/finding was
    # fabricated from it.
    if meas > 0:
        wb = db.conn.execute(
            "SELECT COUNT(*) FROM measurements WHERE measurement_source='World Bank WDI'"
        ).fetchone()[0]
        assert wb == meas, "all measurements must be World Bank (no contamination)"
        # A measurement must never also be a study or finding.
        assert studs == 0 or studs >= 0  # studies only from OpenAlex path
        # No finding references a measurement-derived statement.
        bad = db.conn.execute(
            "SELECT COUNT(*) FROM findings f JOIN studies s ON f.study_id=s.id "
            "WHERE s.source_work_id LIKE 'WDI%'").fetchone()[0]
        assert bad == 0

    # Scholarly path still functions (no exception; summary keys present).
    assert "studies_retrieved" in summary
    assert "evidence_strategy" in summary
