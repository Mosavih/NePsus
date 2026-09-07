"""Investigation Layer — local-LLM extraction (router at call time).

Uses the same OpenAI-compatible local router as Gate 2 (ROUTER_* env, model
EX). Extracts Findings and Interventions from a paper abstract, and assesses
Iran-relevance / adoption barriers for an Intervention. No external paid API.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from openai import OpenAI

from ..gate2_extraction import _get_router_config


def _client() -> OpenAI:
    cfg = _get_router_config()
    if not cfg.get("api_key"):
        raise RuntimeError("ROUTER_API_KEY is not configured.")
    # Hard per-call timeout (2026-09-04: NO pipeline LLM call had one, so a
    # stalled provider queue = a frozen bot / runaway stage. Retry paths
    # already treat 'timeout' as retryable). Env-tunable.
    return OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"],
                  timeout=float(os.environ.get("ROUTER_CALL_TIMEOUT", "300")))


def _chat(system: str, user: str, temperature: float = 0,
         task: str = "draft") -> str:
    """Resilient chat: task -> combo -> router walk with health ledger.

    Default task is draft (judgment work: findings, interventions, relations).
    Pure classifiers pass task="fast". Cache is keyed by the ASSIGNED combo
    (stable across runs); the used route may differ per call.
    """
    from .llm_cache import get as _cache_get, put as _cache_put
    from src.route_health import chat as _rchat
    from .models import combo_for
    primary = combo_for(task)
    cached = _cache_get(primary, system, user)
    if cached is not None:
        return cached
    text, used = _rchat(task,
                        [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
                        temperature=temperature)
    _cache_put(primary, system, user, text)
    return text


def _parse_json_block(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return {}
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {}


_CLS_SYS = (
    "You are a relevance classifier for a research system. Given an INVESTIGATION "
    "QUESTION (and its broader PROBLEM context) and a CANDIDATE STUDY, decide whether "
    "the study provides EVIDENCE relevant to the question's subject matter. "
    "A study is RELEVANT if it bears on the question's TOPIC or SUBJECT DOMAIN -- "
    "e.g. it investigates the same phenomenon, same policy/technology/resource "
    "domain, same outcome (housing supply, vacancy, water scarcity, agricultural "
    "trade, cooperatives), or provides empirical evidence applicable to that "
    "subject -- even if it is not an exact match to the question's wording or uses "
    "a different geographic setting. Topical/subject overlap with the question is "
    "sufficient. A study is NOT RELEVANT only if it addresses a clearly DIFFERENT "
    "problem or subject domain (e.g. quantum physics, pure mathematics, "
    "biochemistry methods) with no bearing on the question's topic. Do not require "
    "an exact lexical match; judge subject-matter relevance. "
    "Return ONLY JSON: "
    '{"relevant": true|false, "reason": str, '
    '"relevance_type": "investigates"|"evaluates_intervention"|'
    '"empirical_evidence"|"mentions_only"|"different_problem"|"background_only"}'
)


def classify_relevance(problem_text: str, study_title: str, study_abstract: str,
                       question_text: str | None = None,
                       entity_names: list | None = None,
                       client: OpenAI | None = None,
                       model: str | None = None) -> dict:
    """Step 1 relevance gate (validated in exp_retrieval_relevance.py).

    Returns {"relevant": bool|None, "reason": str, "relevance_type": str}.
    relevant=None signals an error (caller should treat as NOT RELEVANT to be
    safe, or retry). This is the GATE: NOT RELEVANT studies must never reach
    extraction.

    ANCHORING (Step 16): the gate must answer "Is this study relevant to THIS
    Investigation Question?" -- NOT "relevant to the broad Problem?". Findings
    are linked per-Question, and a paper can be directly on-topic for a Question
    while being semantically distant from the broad Problem wording (e.g. a
    Question about "how bases affect missile vulnerability" while the Problem is
    phrased as "Iran trade decline"). The Step-16 controlled experiment showed
    Problem-anchoring rejected 0/8 genuinely relevant papers (TP=0/8) for such a
    Question, whereas Question-anchoring recovered them (TP=1/8, FP=0/4). When
    `question_text` is supplied it is the PRIMARY anchor; `problem_text` is still
    passed as background context so the gate understands the wider investigation.
    `entity_names` provides light geographic grounding without over-constraining.
    """
    abstract = (study_abstract or "").strip()
    if len(abstract) < 40:
        abstract = "(no abstract available; title only)"
    # Topic anchor (Step 36, option 2 authorized): anchor on the PROBLEM'S SUBJECT
    # DOMAIN -- its salient nouns + geographic entities -- rather than the raw
    # Investigation Question wording (which carries interrogative stems and demands
    # an exact framing, causing 100% false-negative over-rejection per Step 34).
    # The full Question is retained as background context.
    _STOP = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with",
             "is", "are", "was", "were", "be", "by", "at", "as", "that", "this",
             "from", "into", "what", "which", "how", "who", "their", "its", "our",
             "what factors contribute to the", "factors contribute", "rate"}
    topic_words = []
    for w in (problem_text or "").lower().replace("/", " ").replace("?", " ").split():
        w = w.strip(".,;:()\"'")
        if len(w) > 3 and w not in _STOP and w not in topic_words:
            topic_words.append(w)
    topic_phrase = " ".join(topic_words[:12])
    if entity_names:
        topic_phrase = (topic_phrase + " " + " ".join(entity_names)).strip()
    if question_text:
        anchor_label = "INVESTIGATION TOPIC"
        anchor_text = topic_phrase
        background = (
            "FULL INVESTIGATION QUESTION(S) (background):\n" + question_text + "\n"
            + "BROADER PROBLEM CONTEXT:\n" + problem_text + "\n"
            + (("GEOGRAPHIC ENTITIES OF INTEREST: " + ", ".join(entity_names) + "\n")
               if entity_names else "")
        )
    else:
        # Legacy call path (no Question): anchor on the Problem (unchanged).
        anchor_label = "PROBLEM"
        anchor_text = problem_text
        background = ""
    user = (
        anchor_label + ":\n" + anchor_text + "\n\n"
        + background + "\n"
        + "CANDIDATE STUDY TITLE: " + study_title + "\n"
        + "CANDIDATE STUDY ABSTRACT:\n" + abstract[:3000] + "\n\n"
        "Is this study RELEVANT (provides evidence that answers or bears on the "
        + anchor_label.lower() + ")? Judge by SUBJECT-MATTER relevance: a study is "
        "relevant if it investigates the same topic/domain or provides empirical "
        "evidence applicable to the " + anchor_label.lower() + ", even without an "
        "exact wording match or same geography. Only mark NOT RELEVANT if it "
        "addresses a clearly different problem or subject domain. Answer with the "
        "JSON schema."
    )
    try:
        if client is not None:
            from .llm_cache import get as _cg, put as _cp
            _m = model or _get_router_config()["model"]
            cached = _cg(_m, _CLS_SYS, user)
            if cached is not None:
                text = cached
            else:
                resp = client.chat.completions.create(
                    model=_m,
                    messages=[{"role": "system", "content": _CLS_SYS},
                              {"role": "user", "content": user}],
                    temperature=0)
                text = resp.choices[0].message.content or ""
                _cp(_m, _CLS_SYS, user, text)
        else:
            text = _chat(_CLS_SYS, user, temperature=0, task="fast")
    except Exception as e:  # noqa: BLE001
        return {"relevant": None, "reason": f"classifier error: {e}", "relevance_type": "error"}
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx == -1 or end_idx <= start_idx:
            return {"relevant": None, "reason": "unparseable classifier output", "relevance_type": "error"}
        try:
            return json.loads(text[start_idx : end_idx + 1])
        except json.JSONDecodeError:
            return {"relevant": None, "reason": "unparseable classifier output", "relevance_type": "error"}

_FINDING_SYS = (
    "You extract structured Findings from a scientific paper abstract. "
    "Return ONLY JSON: {\"findings\":[{\"statement\":str,\"population\":str,"
    "\"context\":str,\"intervention_name\":str,\"relation\":str,"
    "\"outcome\":str,\"effect_size\":str,"
    "\"causal_strength\":str(\"correlational\"|\"quasi-experimental\"|"
    "\"experimental/RCT\"),\"geographic_applicability\":str,\"limitations\":str}]}. "
    "RULES for intervention_name + relation (Step 2, strict): "
    "Only set intervention_name when the finding provides actual EVIDENCE about "
    "a specific intervention (technology/policy/practice). If the intervention "
    "is merely mentioned, proposed theoretically, or discussed without evidence, "
    "leave intervention_name empty AND relation empty -- do NOT create a link. "
    "relation must be one of: "
    "\"supports\" = finding provides evidence the intervention is EFFECTIVE "
    "(it works / achieves the outcome); "
    "\"evaluates\" = the study tests/assesses the intervention WITHOUT asserting "
    "an effectiveness verdict; "
    "\"reports_failure\" = finding provides evidence of poor performance, "
    "failure, or adverse outcome. "
    "EXAMPLES: \"Smart irrigation reduced water consumption by 23%\" -> "
    "intervention_name=\"smart irrigation\", relation=\"supports\". "
    "\"Smart irrigation was evaluated across 12 farms; yields varied\" -> "
    "relation=\"evaluates\". "
    "\"The smart-irrigation intervention failed to reduce consumption under "
    "drought\" -> relation=\"reports_failure\". "
    "\"Smart irrigation has been proposed as a solution to water scarcity\" -> "
    "intervention_name empty, relation empty (no evidence link). "
    "Do not infer beyond the text. Empty list if none."
)

_INTERVENTION_SYS = (
    "You identify concrete INTERVENTIONS described in a paper abstract. An "
    "intervention MUST be a specific ACTION, TECHNOLOGY, POLICY, PROGRAM, or "
    "PRACTICE that is implemented, proposed, or tested as a REMEDY to change an "
    "outcome (e.g. drip irrigation, managed aquifer recharge, time-of-use "
    "electricity pricing, a cash-transfer program, mobile-banking rollout). "
    "Return ONLY JSON: "
    "{\"interventions\":[{\"name\":str,\"type\":str(\"technology\"|\"policy\"|"
    "\"practice\"),\"description\":str,\"target_problem\":str,"
    "\"adoption_barriers\":str}]}. "
    "STRICT EXCLUSIONS — NONE of these are interventions, do NOT return them: "
    "the word \"none\"; problems/conditions/barriers (e.g. \"factional "
    "rivalries\", \"US restrictions on IMF loans\", \"structural tensions\"); "
    "background/context (e.g. \"relations with the IMF\", \"liberal international "
    "order\"); abstract constructs or independent variables (e.g. \"social "
    "influence\", \"lifestyle compatibility\", \"financial technology "
    "self-efficacy\", \"perceived cost of usage\"); lists of variables/factors "
    "(e.g. \"market size, infrastructure availability\"); discourses or concepts "
    "(e.g. \"legal discourse\", \"justificatory discourse\"); generic events. "
    "If the abstract describes no concrete implemented/proposed remedy, return "
    "an empty list."
)

_IRAN_SYS = (
    "Given an intervention and the country/context of a study, assess its "
    "relevance to Iran and likely adoption barriers. Return ONLY JSON: "
    "{\"relevance_to_iran\":str,\"adoption_barriers\":str}. "
    "Be specific and evidence-minded; do not assert certainty."
)


def extract_findings(abstract: str) -> list[dict]:
    if not abstract or len(abstract) < 30:
        return []
    out = _parse_json_block(_chat(_FINDING_SYS, f"ABSTRACT:\n{abstract[:3500]}"))
    return out.get("findings", []) or []


def extract_interventions(abstract: str) -> list[dict]:
    if not abstract or len(abstract) < 30:
        return []
    out = _parse_json_block(_chat(_INTERVENTION_SYS, f"ABSTRACT:\n{abstract[:3500]}"))
    return out.get("interventions", []) or []


def assess_iran_relevance(intervention_name: str, study_context: str) -> dict:
    prompt = (
        f"INTERVENTION: {intervention_name}\n"
        f"STUDY CONTEXT: {study_context}\n\n"
        "Assess relevance to Iran and adoption barriers."
    )
    return _parse_json_block(_chat(_IRAN_SYS, prompt))
