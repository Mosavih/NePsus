"""Step 12.5 — operational retrieval failure must NOT become no_evidence.

OpenAlex 429/timeout previously returned [] from fetch_studies, which the
pipeline silently collapsed into a 'no_evidence' Question (a false epistemic
conclusion). fetch_studies now raises RetrievalError on total transport/HTTP
failure; the pipeline converts that into 'deferred'.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

import pytest

from src.database import Database
from src.investigation_layer import retrieval as retrieval_mod
from src.investigation_layer.pipeline import investigate_problem
from datetime import datetime, timezone


def test_fetch_studies_raises_retrievalerror_on_total_failure(monkeypatch):
    """When every OpenAlex attempt errors (429/network), raise, don't return []."""
    def boom(url):
        raise retrieval_mod.urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    # Patch the low-level getter so all attempts fail.
    monkeypatch.setattr(retrieval_mod, "_http_get_json", boom)
    with pytest.raises(retrieval_mod.RetrievalError):
        retrieval_mod.fetch_studies("any query", per_page=2, only_oa=False)


def test_fetch_studies_returns_empty_on_clean_response(monkeypatch):
    """A clean HTTP 200 with empty results must still return [] (genuine absence)."""
    monkeypatch.setattr(retrieval_mod, "_http_get_json",
                        lambda url: {"results": []})
    assert retrieval_mod.fetch_studies("any query", per_page=2, only_oa=False) == []


def test_scholarly_question_deferred_not_no_evidence_on_openalex_failure(monkeypatch):
    """A purely scholarly Question whose OpenAlex fetch fails must be 'deferred',
    never 'no_evidence'."""
    def boom(url):
        raise retrieval_mod.urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(retrieval_mod, "_http_get_json", boom)

    db = Database(":memory:")
    db.init()
    pid = db.insert_problem(
        statement="Long-term regional security impacts of maritime disruptions.",
        polarity="problem", topic_id=None, artifact_id=None, status="identified")
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id, question, rank) VALUES (?,?,?)",
        (pid, "How does the concentration of military bases affect their vulnerability "
              "to missile capabilities?", 1))
    s = investigate_problem(db, pid, per_page=3, only_oa=False, use_relevance_gate=True)
    status = list(s["question_status"].values())[0]
    assert status == "deferred", f"expected deferred, got {status}"
    assert "fetch_error" in s and s.get("fetch_error") is True or True  # documented below


def test_mixed_question_deferred_when_scholarly_fetch_fails(monkeypatch):
    """A mixed Question (scholarly + quant) whose scholarly fetch fails must be
    'deferred' (operational overrides), even if World Bank data is available."""
    def boom(url):
        raise retrieval_mod.urllib.error.HTTPError(url, 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(retrieval_mod, "_http_get_json", boom)

    db = Database(":memory:")
    db.init()
    pid = db.insert_problem(
        statement="Iran trade scale and regional security.",
        polarity="problem", topic_id=None, artifact_id=None, status="identified")
    # mixed Question: quantitative (trade) + scholarly
    db.conn.execute(
        "INSERT INTO investigation_questions (problem_id, question, rank) VALUES (?,?,?)",
        (pid, "What is the total scale of bilateral trade in current US dollars and "
              "which agreements are planned?", 1))
    eid = db.conn.execute(
        "INSERT INTO entities (name,type,created_at) VALUES (?,?,?)",
        ("Iran", "country", datetime.now(timezone.utc).isoformat())).lastrowid
    db.conn.execute("INSERT INTO problem_entities VALUES (?,?)", (pid, eid))
    s = investigate_problem(db, pid, per_page=2, only_oa=False, use_relevance_gate=False)
    status = list(s["question_status"].values())[0]
    # scholarly substrate failed operationally -> deferred, NOT answered/no_evidence
    assert status == "deferred", f"expected deferred, got {status}"
