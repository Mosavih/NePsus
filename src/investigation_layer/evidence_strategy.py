"""Step 9 — Lightweight evidence-strategy inference (DECISION LAYER only).

Pure, deterministic function that, given a Problem + its Investigation
Questions, infers what KIND of evidence the problem actually requires BEFORE
any source is queried. This is the minimal architectural response to the Step 9
audit: it lets the Investigation Layer determine its evidence need first, then
(elsewhere) choose where/how to search.

Design constraints honoured:
- NO new LLM call (deterministic keyword signals) -> no rate-limit/scope creep.
- NO schema change, NO new source integration (World Bank etc. deferred).
- Evidence-first preserved: this only classifies the need; retrieval+gating
  unchanged.
- Outcome C from the audit: a lightweight router, not a new table/field/API.

Failure-mode awareness (Step 9 Phase 3): the strategy label helps the pipeline
route to the right SUBSTRATE so that an EVIDENCE-TYPE MISMATCH (C) is not solved
by endlessly optimizing scholarly queries.
"""
from __future__ import annotations

from typing import Dict, List

# Signal lexicons (English, lower-cased substring match).
# QUANT split into STRONG (trade-policy / economics infrastructure -- these mean
# the problem is fundamentally about trade/data) and WEAK (incidental quant
# words that also appear inside scholarly research questions, e.g. "cost" in
# "economic costs", "trade" in "maritime trade"). Only STRONG quant dominates
# the classification, so a research problem mentioning "costs" is not flipped.
_QUANT_STRONG = {
    "trade", "export", "exports", "import", "imports", "bilateral", "transit",
    "pipeline", "tariff", "tariffs", "customs", "gdp", "world bank",
    "central bank", "comtrade", "balance of payments", "merchandise",
    "tonnage", "volumes", "volume", "us$", "dollar", "dollars",
}
_QUANT_WEAK = {
    "cost", "costs", "financial cost", "rebuild", "reconstruction", "data",
    "figures", "indicator", "output", "production",
}
_CURRENT_SIGNALS = {
    "status", "planned", "plan", "meeting", "committee", "recent",
    "ongoing", "now", "latest", "underway", "schedule", "scheduled", "joint",
    "seventh", "negotiation", "negotiations",
    "implementation", "implement", "implemented", "enforce", "enforced",
    "develop", "developing", "launch", "launched", "measure", "measures",
}
# NOTE: 'current' is intentionally EXCLUDED from _CURRENT_SIGNALS. It is a
# currency-unit word ("current US dollars") and an extremely generic adjective,
# and including it promoted quantitative Questions to `mixed`/`current_event`
# (Step 15 audit: "total value ... in current US dollars" was misrouted as a
# current-event need). Valid current-event routing still keys off the signals
# above; and current_event has no adapter, so over-routing there only degrades
# answers to 'deferred'/'unmappable' for no benefit.
_SCHOLAR_SIGNALS = {
    "impact", "impacts", "effect", "effects", "long-term", "evidence",
    "study", "studies", "literature", "mechanism", "mechanisms", "why",
    "causal", "evaluate", "evaluation", "assess", "assessment", "extent",
    "correlat", "trend", "trends", "implication", "implications",
    # Causal / mechanism phrasing (Step 15): "how does X affect Y",
    # "vulnerability", "what determines/factors/drives" signal a research
    # dimension that must not be silently dropped when a Question also asks
    # for quantitative data (compound Question must keep BOTH substrates).
    "affect", "affects", "vulnerab", "determine", "drive", "drives",
    "factors", "relationship", "relation",
}


def _hits(text: str, lexicon) -> List[str]:
    t = text.lower()
    return sorted({s for s in lexicon if s in t})


def infer_evidence_strategy(problem: Dict, questions: List[str],
                            per_question: bool = False) -> Dict:
    """Return the inferred evidence strategy for a Problem.

    problem: dict with 'statement' (and optionally 'topic', 'entity_names').
    questions: list of Investigation Question strings.
    per_question: when True, classify ONLY the given Question text(s) and
        ignore the parent Problem statement. Required for per-Question routing
        (Step 11): the parent statement often contains cross-cutting words
        (e.g. "trade") that would otherwise leak into and misroute an
        individual Question (e.g. a scholarly "military bases" Question).

    Returns {
      'strategy': one of scholarly_study | quantitative_official |
                  current_event | mixed,
      'rationale': human-readable reason,
      'signals': {'quantitative': [...], 'current': [...], 'scholarly': [...]},
    }
    """
    if per_question:
        # Classify the Question in isolation -- do NOT inherit the parent
        # Problem statement (which may contain unrelated cross-cutting words).
        blob = " ".join(list(questions))
    else:
        blob = " ".join([problem.get("statement", "")] + list(questions))
    q_strong_hits = _hits(blob, _QUANT_STRONG)
    q_weak_hits = _hits(blob, _QUANT_WEAK)
    q_hits = sorted(set(q_strong_hits) | set(q_weak_hits))
    c_hits = _hits(blob, _CURRENT_SIGNALS)
    s_hits = _hits(blob, _SCHOLAR_SIGNALS)

    n_qs, n_qw, n_c, n_s = len(q_strong_hits), len(q_weak_hits), len(c_hits), len(s_hits)

    # Step 11 fix (under-routing): the previous rule let a SINGLE scholarly
    # signal (e.g. "impact") dominate and flip mixed/current/quant Questions to
    # scholarly_study, so they silently missed their quantitative/current
    # substrate. New rule:
    #  - "scholarly_study" only when scholarly signals CLEARLY dominate
    #    (n_s > n_qs + n_c + 1) AND no strong-quant/current signal present.
    #  - otherwise, ANY co-occurring strong-quant or current signal promotes the
    #    label to mixed/quant/current (a Question may need several substrates).
    #  - strong-quant present -> quantitative_official (mixed if current too).
    #  - current-only (no scholarly dominance, no strong quant) -> current_event.
    if n_qs >= 1:
        if n_c >= 1:
            strategy = "mixed"
            rationale = ("requires both current/policy status AND quantitative "
                        "trade or economic data (research alone is insufficient)")
        elif n_s >= 1:
            # Compound Question: quantitative data AND a research/causal dimension
            # (e.g. "what are exports AND how do bases affect vulnerability?").
            # Step 15: must keep BOTH substrates so the scholarly half is not
            # silently dropped by routing to quantitative_official only.
            strategy = "mixed"
            rationale = ("demands quantitative data AND scholarly research on a "
                        "causal/mechanism dimension -> route to both substrates")
        else:
            strategy = "quantitative_official"
            rationale = ("demands quantitative/official trade or economic data, "
                        "not scholarly research")
    elif n_qw >= 1:
        # A quantifiable magnitude word (cost, rebuild, financial cost, ...) even
        # if "weak" means the Question needs DATA alongside research/current
        # evidence -- promote to mixed rather than leaving it scholarly-only
        # (Step 11 fix: under-routing of magnitude Questions).
        strategy = "mixed"
        rationale = ("asks for a magnitude/scale (cost, rebuild, financial) that "
                    "requires quantitative data as well as research/current evidence")
    elif n_c >= 1 and n_s <= n_c + 1:
        # current/policy signal present and not clearly outweighed by research
        strategy = "current_event"
        rationale = ("demands current-event / policy / official-report evidence "
                    "more than research")
    elif n_qw >= 1 and n_s >= 1:
        # A compound Question: asks for a magnitude/scale (weak-quant) AND has a
        # scholarly research dimension -> needs BOTH substrates (Step 15 audit:
        # a "quantitative + how/why" Question must not silently lose its
        # scholarly half by being routed quantitative_official only).
        strategy = "mixed"
        rationale = ("compound Question: a scale/magnitude need AND a research "
                    "dimension -> route to quantitative AND scholarly substrates")
    elif n_s > n_qs + n_c + 1:
        # scholarly clearly dominates; quant/current are incidental noise
        strategy = "scholarly_study"
        rationale = ("general scholarly literature is the appropriate primary "
                    "substrate (research on impacts/effects/mechanisms)")
    elif n_c >= 1 and n_s >= 1:
        # Genuine co-occurrence: a current/policy signal AND a research/causal
        # dimension -> route to both (current_event + scholarly). NOTE: a
        # scholarly-only signal (e.g. "affect") must NOT escalate to mixed on its
        # own -- that would inject a spurious quant/current substrate (Step 17
        # audit: "phase of the moon affect wheat yield" was wrongly routed
        # mixed, then failed its phantom quant leg). mixed requires a real
        # quant/current signal alongside the scholarly one.
        strategy = "mixed"
        rationale = ("mixed evidence needs (research + current/policy); "
                     "route to all applicable substrates")
    else:
        strategy = "scholarly_study"
        rationale = ("no specific quantitative/current signal; general "
                    "scholarly literature is the appropriate default substrate")

    return {
        "strategy": strategy,
        "rationale": rationale,
        "signals": {"quantitative": q_hits, "current": c_hits, "scholarly": s_hits},
    }
