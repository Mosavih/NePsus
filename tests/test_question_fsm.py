"""Step 15 — Question lifecycle FSM audit + fixes.

Verifies:
- World Bank adapter exception is treated as OPERATIONAL (deferred), never a
  crash that strands Questions in 'pending' (the demonstrated FSM violation).
- A Question can never remain permanently 'pending' after a run.
- Operational failure (WB/scholarly) -> deferred, never no_evidence/answered.
- current_event strategy -> unmappable (no adapter).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

import pytest

from src.database import Database
from src.investigation_layer import pipeline as PL
from src.investigation_layer import worldbank as wb, retrieval as ret


def _make_db(monkeypatch, wb_stub, fetch_stub=None, questions=None):
    if wb_stub is not None:
        monkeypatch.setattr(wb, "fetch_indicator_observations", wb_stub)
    if fetch_stub is not None:
        monkeypatch.setattr(ret, "fetch_studies", fetch_stub)
    db = Database(":memory:")
    db.init()
    pid = db.insert_problem(
        statement="Iran trade and security.", polarity="problem",
        topic_id=None, artifact_id=None, status="identified")
    for i, q in enumerate(questions, 1):
        db.conn.execute(
            "INSERT INTO investigation_questions (problem_id,question,rank) VALUES (?,?,?)",
            (pid, q, i))
    eid = db.conn.execute(
        "INSERT INTO entities (name,type,created_at) VALUES (?,?,?)",
        ("Iran", "country", "2026-01-01T00:00:00Z")).lastrowid
    db.conn.execute("INSERT INTO problem_entities VALUES (?,?)", (pid, eid))
    return db, pid


def test_wb_adapter_exception_is_operational_not_pending(monkeypatch):
    """A World Bank fetch that raises must NOT crash the run nor strand the
    Question in 'pending' -- it becomes 'deferred' (operational)."""
    def kaboom(country, code, per_page=5):
        raise RuntimeError("WB adapter exploded")
    db, pid = _make_db(
        monkeypatch, kaboom,
        questions=["What is the value of Iran exports in US dollars?"])
    # No exception should escape; run finalizes.
    s = PL.investigate_problem(db, pid, per_page=3, only_oa=False, use_relevance_gate=False)
    assert s.get("wb_operational_errors", 0) >= 1
    r = db.conn.execute(
        "SELECT evidence_strategy, status FROM investigation_questions").fetchone()
    assert r["evidence_strategy"] == "quantitative_official"
    assert r["status"] == "deferred"          # operational, NOT pending/no_evidence
    assert r["status"] != "pending"


def test_no_question_left_permanently_pending(monkeypatch):
    """Across strategies, no Question ends a run in 'pending'."""
    def ok_wb(c, code, per_page=5):
        return {"status": "ok", "observations": [{"date": "2023", "value": 1.0}], "meta": {}} if c == "IR" else {"status": "source_unavailable", "error": "x"}
    db, pid = _make_db(
        monkeypatch, ok_wb,
        questions=[
            "What is the value of Iran exports in US dollars?",
            "How do military base concentrations affect missile vulnerability?",
            "What are Iran exports and how do bases affect missiles?",
            "What current agreements were signed this week?",
        ])
    PL.investigate_problem(db, pid, per_page=3, only_oa=False, use_relevance_gate=False)
    pending = db.conn.execute(
        "SELECT COUNT(*) FROM investigation_questions WHERE status='pending'").fetchone()[0]
    assert pending == 0


def test_current_event_strategy_is_unmappable(monkeypatch):
    def kaboom(c, code, per_page=5):
        raise RuntimeError("x")
    db, pid = _make_db(
        monkeypatch, kaboom,
        questions=["What measures are planned at the upcoming joint meeting "
                   "between Iran and Saudi Arabia?"])
    PL.investigate_problem(db, pid, per_page=3, only_oa=False, use_relevance_gate=False)
    r = db.conn.execute(
        "SELECT evidence_strategy, status FROM investigation_questions").fetchone()
    assert r["evidence_strategy"] == "current_event"
    assert r["status"] == "unmappable"


def test_operational_failure_is_deferred_not_no_evidence(monkeypatch):
    """Scholarly substrate erroring -> 'deferred', never 'no_evidence'.

    NOTE: pipeline does `from .retrieval import fetch_studies`, so the bound name
    must be patched on the pipeline module (PL), not on retrieval. This test is
    source-independent: it forces the operational path regardless of OpenAlex
    reachability (which fluctuates in CI/sandbox).
    """
    def boom(url, per_page=5):
        raise __import__("urllib.error", fromlist=["HTTPError"]).HTTPError(
            "u", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(PL, "fetch_studies", boom)
    db, pid = _make_db(
        monkeypatch,
        lambda c, code, per_page=5: {"status": "source_unavailable", "error": "x"},
        questions=["How do military base concentrations affect missile vulnerability?"])
    PL.investigate_problem(db, pid, per_page=3, only_oa=False, use_relevance_gate=False)
    r = db.conn.execute(
        "SELECT status FROM investigation_questions").fetchone()
    assert r["status"] == "deferred"
    assert r["status"] != "no_evidence"
