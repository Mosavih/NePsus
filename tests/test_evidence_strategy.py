"""Tests for Step 9 evidence-strategy inference (pure, deterministic)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from src.investigation_layer.evidence_strategy import infer_evidence_strategy


def test_scholarly_problem():
    # Purely scholarly: long-term security impacts, mechanisms, causal effects
    # (NO magnitude/current words) -> scholarly_study. A magnitude word ("costs")
    # would now correctly promote to mixed, so we keep this example clean.
    p = {"statement": "Military conflict has caused infrastructure damage and "
                      "regional instability."}
    qs = ["What are the long-term regional security impacts of the damaged "
          "military installations?",
          "How does the disruption affect the mechanisms of maritime security?",
          "What causal factors explain the escalation of regional tensions?"]
    r = infer_evidence_strategy(p, qs)
    assert r["strategy"] == "scholarly_study"
    assert "scholar" in r["rationale"].lower()


def test_magnitude_question_promotes_to_mixed():
    # Step 11: a magnitude question (costs/rebuild) needs DATA alongside
    # research -> mixed, NOT scholarly-only (the under-routing bug).
    p = {"statement": "Conflict damaged infrastructure."}
    qs = ["What are the total economic and logistical costs incurred by the "
          "involved nations following the conflict?"]
    r = infer_evidence_strategy(p, qs)
    assert r["strategy"] == "mixed"


def test_quantitative_trade_problem():
    # P2 Iran-Afghanistan trade: current transit/export obstacles -> quantitative.
    p = {"statement": "Obstacles and barriers to bilateral trade, transit, and "
                      "economic relations between Iran and Afghanistan."}
    qs = ["What specific transit and export obstacles currently impede trade "
          "between Iran and Afghanistan?",
          "What measures are planned for the joint committee to address these "
          "barriers?"]
    r = infer_evidence_strategy(p, qs)
    # trade + current + committee => mixed (quantitative + current)
    assert r["strategy"] in ("quantitative_official", "mixed")
    assert "trade" in r["signals"]["quantitative"] or "bilateral" in r["signals"]["quantitative"]


def test_current_event_problem():
    p = {"statement": "Development of economic cooperation and the expansion of "
                      "commercial relations."}
    qs = ["What is the current status of the bilateral cooperation projects?",
          "What measures are planned at the upcoming joint meeting?"]
    r = infer_evidence_strategy(p, qs)
    assert r["strategy"] in ("current_event", "mixed")
    assert r["signals"]["current"]


def test_mixed_health_problem():
    # P5 healthcare: extent of damage (weak quant) + mechanisms to enforce medical
    # neutrality (current/policy). The *gap* is IR-specific current-event / official
    # evidence (general health-systems research already exists), so current_event
    # or mixed are both correct routing signals.
    p = {"statement": "Systematic destruction of healthcare infrastructure and "
                      "erosion of medical neutrality during conflict."}
    qs = ["What is the total financial cost required to rebuild the damaged "
          "healthcare facilities?",
          "What specific mechanisms are implemented to enforce medical "
          "neutrality under international law?"]
    r = infer_evidence_strategy(p, qs)
    assert r["strategy"] in ("current_event", "mixed")
    assert r["signals"]["current"]


def test_no_signal_defaults_scholarly():
    p = {"statement": "A situation exists that requires attention."}
    r = infer_evidence_strategy(p, ["Is there a problem?"])
    assert r["strategy"] == "scholarly_study"
