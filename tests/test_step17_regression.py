"""Step 17 regression — classify_evidence_strategy must not over-escalate.

Step 16 added causal verbs (affect/impact/...) to _SCHOLAR_SIGNALS to catch
causal scholarly Questions. Step 17 caught the side effect: a PURELY scholarly
Question (only a scholarly signal, no quant/current signal) was wrongly
escalated to 'mixed', injecting a phantom quant substrate that then failed ->
a false operational/deferred or unmappable outcome instead of the correct
scholarly_study / no_evidence. 'mixed' must require a genuine quant/current
signal co-occurring with the scholarly one.
"""
from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))

from src.investigation_layer.evidence_strategy import infer_evidence_strategy


def _route(q: str) -> str:
    return infer_evidence_strategy(
        {"statement": "x", "question_texts": [q]}, [q], per_question=True
    )["strategy"]


def test_pure_scholarly_stays_scholarly_not_mixed():
    # "affect" is a scholarly signal (added Step 16); with NO quant/current
    # signal it must remain scholarly_study, never mixed.
    assert _route("Does the phase of the moon affect Iran wheat yield?") == "scholarly_study"
    assert _route("How do military bases affect missile vulnerability?") == "scholarly_study"


def test_compound_quant_plus_causal_is_mixed():
    # genuine co-occurrence: strong quant + causal scholarly -> mixed (both legs).
    assert _route(
        "What are Iran exports in US dollars AND how do bases affect missile vulnerability?"
    ) == "mixed"


def test_current_plus_causal_is_mixed():
    assert _route(
        "What current plan affects Iran export vulnerability?"
    ) == "mixed"


def test_strong_quant_without_scholarly_is_quantitative():
    assert _route("What is the total value of Iran merchandise exports in US dollars?") == "quantitative_official"
