"""Step 15 audit regression tests: routing correctness (semantic chain).

Two concrete failures the end-to-end audit demonstrated:
- F1: a compound Question ("exports in US dollars AND how do bases affect
  vulnerability?") was routed quantitative_official and silently LOST its
  scholarly half. Compound quant+scholarly must route 'mixed' (both substrates).
- F2: "total value of Iran merchandise exports in current US dollars" was
  misrouted 'mixed' because the currency word "current" matched _CURRENT_SIGNALS.
  "current" is now excluded; such a Question routes quantitative_official and is
  NOT treated as a current_event need.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from src.investigation_layer.evidence_strategy import infer_evidence_strategy


def _q(text):
    return infer_evidence_strategy(
        {"statement": "x", "question_texts": [text], "entity_names": ["Iran"]},
        [text], per_question=True)["strategy"]


def test_compound_quant_scholarly_routes_mixed():
    # "exports" (strong quant) + "how ... affect ... vulnerability" (scholarly)
    # must keep BOTH substrates -> mixed, never quantitative_official-only.
    assert _q("What are Iran exports in US dollars and how do bases affect "
              "missile vulnerability?") == "mixed"


def test_currency_current_not_misrouted_as_current_event():
    # "current US dollars" -> the word "current" is the currency unit, NOT a
    # current-event signal. Must be quantitative_official, not mixed/current_event.
    assert _q("What is the total value of Iran merchandise exports in current "
              "US dollars?") == "quantitative_official"


def test_pure_scholarly_stays_scholarly():
    assert _q("How does the concentration of military bases affect their "
              "vulnerability to missiles?") == "scholarly_study"


def test_genuine_current_event_still_routes_current():
    # Real current-event phrasing (planned / meeting) still routes current_event.
    assert _q("What measures are planned at the upcoming joint meeting between "
              "Iran and Saudi Arabia?") == "current_event"
