"""
Nexus-Think-Tank — Gate 2: Extraction & Integrity

Gate 2 converts a pending SourceArtifact into structured objects:

    SourceArtifact
        ├── Claims
        ├── Measurements
        ├── Entities
        └── Problem Signals  (v1.9 Discovery Layer)

Design principles
-----------------
1. Gate 2 extracts what the source explicitly contains.
2. Gate 2 does NOT infer facts, events, relationships, or analytical claims.
3. Claims preserve provenance through artifact_id and are marked as source claims.
4. Measurements are first-class objects, not merely claim_type="measurement".
5. Entities are extracted as canonical candidates and linked to the artifact.
6. Entity resolution beyond exact matching belongs to Gate 3.
7. If extraction fails, the artifact remains PENDING rather than being falsely
   marked as accepted or extracted.
8. No API key means Gate 2 does nothing; raw artifacts remain pending.

The database layer is treated as the storage contract. This module therefore
uses the existing Database API rather than issuing its own SQL.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from openai import OpenAI

from .database import Database
from .models import ProcessingStatus
from .investigation_layer.iran_gate import classify_problem_iran


# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_MODEL = "C1"

MAX_TOKENS = 3072
TEMPERATURE = 0.2

# Maximum number of artifacts processed in one Gate 2 invocation.
BATCH_SIZE = 10

# Maximum article text sent to the model.
# Gate 1 already stores the complete cleaned article.
MAX_CONTENT_CHARS = 12000


# ============================================================================
# CONTROLLED VOCABULARIES
# ============================================================================

ALLOWED_CLAIM_TYPES = {
    "fact",
    "statement",
    "prediction",
    "observation",
    "causal",
    "relationship",
}

ALLOWED_TEMPORAL_SCOPES = {
    "past",
    "present",
    "future",
}

ALLOWED_ENTITY_TYPES = {
    "person",
    "organization",
    "company",
    "country",
    "institution",
    "facility",
    "policy",
}


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

_SYSTEM_PROMPT = """
You are the extraction component of an information-analysis system.

Your task is to extract ONLY information explicitly expressed in the supplied
source document.

Return exactly one JSON object with these keys:

{
  "claims": [],
  "measurements": [],
  "entities": [],
  "problem_signals": []
}

------------------------------------------------------------
1. CLAIMS
------------------------------------------------------------

A Claim is a proposition expressed by the source.

Extract substantive propositions that the source presents as information,
statements, observations, predictions, causal explanations, or relationships.

# ============================================================================
# CLAIMS FROZEN AS v0 (post Step 4 pin-corpus experiment)
# ============================================================================
# The Claim extraction is frozen at this prompt. The Step 4 experiment (pinned
# corpus, 96 -> 80 claims) showed the syntactic-vs-epistemic gap is PROMPT-
# ADDRESSABLE, not schema-urgent: suppressing generic norms / universal
# aphorisms / rhetorical-ceremonial language / pleasantries / editorial asides
# removed the off-target claims while preserving every substantive proposition.
#
# What is frozen:
#   - A Claim is a proposition the source expressed about the world (ontology
#     section 3.1.5). Gate 2 extracts it from the article text only.
#
# Known v0 limitations (documented, not yet fixed - by design):
#   - POLARITY / MODALITY is not captured. Denials and negations
#     (e.g. "BRICS is not pursuing a common currency") are flattened into the
#     "fact" claim_type with no polarity marker, so a negation reads as an
#     assertion.
#   - NORMATIVE vs DESCRIPTIVE is not distinguished. General principles the
#     source invokes are suppressible by prompt, but a genuine normative
#     proposition the source specifically asserts is still a plain "statement".
#   - These are DEFERRED ONTOLOGY DESIGN QUESTIONS (a polarity/modality or
#     normative-proposition distinction), not bugs to patch in the prompt.
#
# Future evaluation: decide, from real analytical need (not prompt convenience),
# whether polarity/modality/normative status belong as Claim subtypes or as
# separate ontology objects, BEFORE changing the schema.

Each claim must contain:

{
  "proposition": "...",
  "claim_type": "...",
  "author_source": "...",
  "time": "...",
  "temporal_scope": "..."
}

Rules:

- proposition must be a single self-contained sentence.
- Preserve the source's meaning and framing.
- Do NOT add interpretation.
- Do NOT combine unrelated propositions.
- Do NOT create a claim merely because a number appears in the article.
  Quantitative observations belong in measurements.
- Do NOT use "measurement" as claim_type.

Do NOT create a claim for the following (they are not source-specific propositions
about the world and do not belong in the knowledge base):

- Generic normative principles or universal aphorisms
  (e.g. "stability is unattainable without peace and respect for international law",
   "medical personnel must be protected under the Geneva Conventions").
  These are general norms/legal principles, not propositions the source asserted
  about a specific occurrence.
- Rhetorical or ceremonial language
  (e.g. "relations must be further strengthened in practice",
   "talks reflect a continued commitment", "this approach should be evident in
   future agreements"). Boilerplate that carries no new, specific information.
- Generic pleasantries or social filler
  (e.g. "the players enjoyed the sun and the fans were happy",
   "everyone agreed it was a fun day").
- Non-source-specific editorial formulations or presentist asides
  (e.g. "this situation persists to this day", sweeping generalizations not
  grounded in a specific statement by the source).

Preserve substantive propositions: factual events, specific statements by named
actors, measurements already captured elsewhere, causal claims, concrete
diplomatic/economic facts, and dated occurrences. The goal is a smaller set of
claims that are genuinely about the world, not merely fewer claims.

Allowed claim_type values:

- fact
- statement
- prediction
- observation
- causal
- relationship

author_source:
- Identify the person, organization, institution, or source explicitly
  presented as making the claim.
- If no attribution is given, return null.

time:
- The time at which the claim was made/issued, if explicitly stated.
- Do not invent a date.
- Return null if unavailable.

temporal_scope:
- "past" if the proposition concerns a completed past occurrence.
- "present" if it concerns a current condition or activity.
- "future" if it concerns something expected/planned to happen.
- If genuinely unclear, return null.

------------------------------------------------------------
2. MEASUREMENTS
------------------------------------------------------------

A Measurement is a quantitative observation explicitly contained in the
source.

Extract:

- percentages
- counts
- monetary amounts
- rates
- quantities
- physical measurements
- statistical indicators
- numerical economic indicators

SEMANTIC FILTERING (v0, post Step 5 pin-corpus experiment):

- Extract ONLY quantitative observations the source explicitly states.
- Do NOT treat every number as a measurement.
- DO extract: counts, percentages, monetary amounts, rates, quantities, ratios,
  durations, physical dimensions, and other quantitative observations when they
  describe a measurable property or state.
- Do NOT extract: identification numbers, registration numbers, document
  numbers, phone numbers, codes, IDs, addresses, article numbers, or similar
  identifiers.
- Do NOT treat dates or years describing when an entity was born, founded,
  established, signed, etc. as measurements (e.g. a birth year is not a
  measurement).
- Do NOT calculate or derive values the source does not explicitly state.
  Never derive quantities through arithmetic (e.g. do not compute 25% of 416,000
  wells = 104,000 wells; extract only what the source states).
- When a sentence contains multiple explicitly stated atomic quantitative
  observations, extract each atomic observation ONLY ONCE.
- Do NOT extract the same quantitative fact multiple times merely because it is
  repeated, paraphrased, or expressed with a synonym.
- PRESERVE distinct measurements when they refer to different subjects, scopes,
  periods, or quantities, even if their numeric values are identical.
- Extract reference_period ONLY when the source explicitly provides or clearly
  attaches a period to the measurement. Never infer it from the publication date.
- For comparative statements, preserve the comparison in the indicator /
  description as the source supports; do NOT invent a comparison-target entity
  merely to populate subject_entity.

Each measurement must contain:

{
  "indicator": "...",
  "subject_entity": "...",
  "value": 0,
  "unit": "...",
  "reference_period": "...",
  "measurement_time": "..."
}

indicator:
- A concise description of WHAT is being measured.

subject_entity:
- The NAMED ENTITY that the measurement is explicitly and directly about, if one
  exists and is also appropriate (e.g. "Iran Chamber of Commerce").
- Use the canonical entity name when that entity is also in the entities list.
- Return null when the measurement is NOT about a specific named entity:
  - Do NOT use generic categories or domains ("state", "private sector",
    "agricultural water wells", "the economy").
  - Do NOT use geographic or global aggregates ("global goods", "the world",
    "international trade").
  - Do NOT use an event, process, or conflict as the subject ("US-Israeli
    aggression against Iran") - that belongs to the event layer, not an Entity.
  - Do NOT attach a nearby proper noun merely because it appears in the same
    sentence (e.g. the title of an exhibition, law, article, or concept such as
    "Idea" when the measurement is about the participating artists).
  - Do NOT attach the title/name of an exhibition, law, article, or abstract
    concept unless the measurement is genuinely about that entity.
- If no appropriate named entity exists, return null. The measurement is still
  retained; only its subject linkage is left empty.

value:
- Numeric value only.

unit:
- The actual unit, such as "%", "IRR", "USD", "barrels/day",
  "people", "wells", "tons".
- Do not invent units.

reference_period:
- The period covered by the measurement, if stated.
- Examples: "2025", "Q1 2026", "January 2026",
  "since 2020", "Seventh Development Plan".
- Do not convert vague periods into invented calendar dates.
- Return null if no reference period is stated.

measurement_time:
- When the measurement was made or reported, if explicitly stated.
- Do not confuse this with the reference period.
- Return null if unavailable.

Important:
A sentence such as "25 percent of wells have smart meters" can produce:
1. a Measurement representing 25%, and
2. a Claim representing the source's proposition,
if both are substantively useful.
Do not create multiple redundant measurements for the same number.

------------------------------------------------------------
4. PROBLEM SIGNALS  (Discovery Layer — ontology v1.9)
------------------------------------------------------------

# ============================================================================
# PROBLEM SIGNALS FROZEN AS v0 (post pin-corpus implementation, 2026-08-15)
# ============================================================================
# This is the initial Discovery-Layer problem-identification capability.
# Design limits of v0 (documented, not yet addressed):
#   - Ephemeral scope: a Problem signal is derived per-article from that
#     article's own claims/entities. No cross-article aggregation yet
#     (multiple articles on the same problem remain separate Problems).
#   - Evidence is the originating artifact only (problem_evidence type
#     'artifact'). Claim/Measurement-level evidence linking is not yet wired.
#   - Affected entities are linked ONLY when they exactly match an entity
#     extracted from the SAME article. Cross-article / alias resolution is
#     NOT applied (see Entity audit: aliases recorded but not merged).
#   - A Problem requires >=1 evidence reference by construction (the
#     artifact), so empty/orphan problems cannot be created.
#   - polarity is explicit (problem | opportunity) to avoid treating every
#     negative claim as a crisis.
# Future work (NOT v0): cross-article problem aggregation/merge, Claim- and
# Measurement-level evidence links, entity-identity resolution, and handoff
# to the Investigation Layer (OpenAlex + local LLM). Do not expand scope
# until these are deliberately designed.

In addition to Claims/Entities, identify 0 to N structured PROBLEM SIGNALS
worth investigating. A problem signal is a persistent or developing condition
(e.g. a structural problem, a risk, or an actionable opportunity), NOT a single
discrete event and NOT a mere negative-sounding sentence.

Epistemic guardrail (prevents inflation):
- A problem signal MUST be grounded in the article's extracted claims/entities.
- It MUST imply concrete investigation questions (what to study), not answers.
- Use polarity: "problem" for adverse/risky conditions, "opportunity" for
  beneficial/actionable developments.
- Do NOT emit a problem signal for every negative claim. Many claims may map
  to one problem; a one-off event is an Event, not a Problem.
- Do NOT invent affected entities. Reference only entities already extracted
  from this article (by their canonical name).

Each problem signal object:
{
  "statement": "neutral description of the condition",
  "polarity": "problem" | "opportunity",
  "topic": "coarse theme (e.g. water, labor, energy, health, technology)",
  "affected_entities": ["canonical entity name 1", ...],   // only from extracted entities
  "investigation_questions": [
    "question 1 (what should we study?)",
    "question 2",
    "question 3"
  ]
}

Return an empty list [] if the article contains no conditions worth a dedicated
investigation signal.

------------------------------------------------------------
3. ENTITIES
------------------------------------------------------------

Extract persistent identifiable entities explicitly mentioned in the source.

Examples:

- people
- organizations
- companies
- countries
- institutions
- facilities
- policies

Each entity must contain:

{
  "name": "...",
  "entity_type": "...",
  "aliases": []
}

Allowed entity_type values:

- person
- organization
- company
- country
- institution
- facility
- policy

Use a canonical name when it can be determined directly from the source.

aliases:
- Alternative names, abbreviations, acronyms, or names explicitly used
  in the same document.
- Do not invent aliases.

Do NOT create entities for generic concepts such as:
"agricultural sector", "water", "inflation", "the government",
unless the text clearly identifies them as a persistent entity.

Salience and relevance:
- This is an intelligence-analysis knowledge base. Extract only entities that
  are substantively central to the article's analytical content: the principal
  actors, institutions, locations, policies, and instruments it turns on.
- Do NOT extract entities that are merely incidental, decorative, or passing
  mentions with no analytical weight (e.g. a person named only in a quote
  attribution, a courtesy title, or a one-off example).

No incidental-list enumeration:
- If an article contains a roster, list, or enumeration of individuals
  (e.g. "30 artists in an exhibition", an attendee list, a name-drop parade),
  do NOT extract every listed person. Extract the organizing entity instead
  (the exhibition, the institution, the event) and omit the enumerated members
  unless a specific member is independently significant to the analysis.
- Enumerating incidental lists floods the knowledge base with low-value,
  non-reusable entities and must be avoided.

------------------------------------------------------------
GENERAL RULES
------------------------------------------------------------

- Extract only explicitly supported information.
- Never infer missing dates, values, identities, relationships, or causes.
- Do not hallucinate.
- Do not write explanations outside the JSON object.
- Use null where information is unavailable.
- Return valid JSON only.
"""


# ============================================================================
# ROUTER CONFIGURATION
# ============================================================================

def _get_router_config() -> dict[str, str]:
    """
    Read router configuration at call time.

    This is intentional: run_phase1.py loads .env before invoking Gate 2,
    so reading environment variables here avoids stale configuration.
    """
    from src.investigation_layer.models import combo_for
    return {
        "base_url": os.environ.get(
            "ROUTER_BASE_URL",
            "http://localhost:20128/v1",
        ),
        "api_key": os.environ.get("ROUTER_API_KEY", ""),
        "model": os.environ.get("ROUTER_COMBO_EXTRACT", combo_for("extract")),
    }


def _route_candidates() -> list[str]:
    """Extraction route order: assigned combo, then other combos, then legacy
    aliases — deduped, order preserved."""
    cfg = _get_router_config()
    chain: list[str] = []
    try:
        from src.route_health import combo_candidates, FALLBACK_ALIASES
        chain += combo_candidates(cfg["model"]) + FALLBACK_ALIASES
    except Exception:
        try:
            from src.investigation_layer.models import FALLBACK_CHAIN as _fc
            chain += _fc
        except Exception:
            pass
    out, seen = [cfg["model"]], {cfg["model"]}
    for m in chain:
        if m and m not in seen:
            out.append(m)
            seen.add(m)
    return out


def _build_router_client(route: str, timeout: float):
    cfg = _get_router_config()
    return OpenAI(base_url=cfg["base_url"], api_key=cfg["api_key"],
                  timeout=timeout)


# ============================================================================
# NORMALIZATION HELPERS
# ============================================================================

def _clean_string(value: Any) -> Optional[str]:
    """
    Normalize an LLM string value.

    Returns None for missing/empty values.
    Collapses whitespace and removes common line-break artefacts.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = re.sub(r"\s+", " ", value).strip()

    return value if value else None


def _normalize_claim_type(value: Any) -> str:
    """Return a controlled claim type."""
    value = _clean_string(value)

    if not value:
        return "statement"

    value = value.lower()

    # The model must not classify quantitative observations as a separate
    # "measurement" claim because Measurement is a first-class object.
    if value == "measurement":
        return "observation"

    if value not in ALLOWED_CLAIM_TYPES:
        return "statement"

    return value


def _normalize_temporal_scope(value: Any) -> Optional[str]:
    """Return a controlled temporal scope."""
    value = _clean_string(value)

    if not value:
        return None

    value = value.lower()

    if value in ALLOWED_TEMPORAL_SCOPES:
        return value

    return None


def _normalize_entity_type(value: Any) -> str:
    """Return a controlled entity type."""
    value = _clean_string(value)

    if not value:
        return "organization"

    value = value.lower()

    aliases = {
        "org": "organization",
        "ngo": "organization",
        "government": "institution",
        "government body": "institution",
        "agency": "institution",
        "personnel": "person",
        "company": "company",
        "corporation": "company",
        "firm": "company",
        "state": "country",
        "nation": "country",
        "building": "facility",
        "infrastructure": "facility",
        "law": "policy",
        "legislation": "policy",
    }

    value = aliases.get(value, value)

    if value in ALLOWED_ENTITY_TYPES:
        return value

    # Conservative fallback.
    return "organization"


def _safe_float(value: Any) -> Optional[float]:
    """Convert a model value to float without raising."""
    if value is None:
        return None

    if isinstance(value, bool):
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_aliases(value: Any) -> list[str]:
    """Normalize an entity alias list."""
    if not isinstance(value, list):
        return []

    aliases: list[str] = []

    for item in value:
        cleaned = _clean_string(item)

        if cleaned and cleaned not in aliases:
            aliases.append(cleaned)

    return aliases


def _parse_datetime(value: Any) -> Optional[datetime]:
    """
    Parse an ISO datetime supplied by the LLM.

    Gate 2 does not invent dates. Invalid or ambiguous values become None.
    """
    value = _clean_string(value)

    if not value:
        return None

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ============================================================================
# JSON PARSING
# ============================================================================

def _empty_extraction() -> dict[str, list]:
    return {
        "claims": [],
        "measurements": [],
        "entities": [],
        "problem_signals": [],
    }


def _parse_llm_json(raw_text: str) -> dict[str, list]:
    """
    Parse and minimally validate the LLM response.

    The router is instructed to return JSON, but this function still handles
    occasional Markdown fences or surrounding text defensively.
    """
    if not raw_text:
        return _empty_extraction()

    text = raw_text.strip()

    # Remove Markdown code fences if a model adds them.
    if text.startswith("```"):
        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s*```$", "", text)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Conservative fallback: locate the outermost JSON object.
        start = text.find("{")
        end = text.rfind("}")

        if start == -1 or end <= start:
            raise ValueError("LLM response did not contain valid JSON.")

        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(
                "LLM response contained malformed JSON."
            ) from exc

    if not isinstance(data, dict):
        raise ValueError("LLM response JSON must be an object.")

    result = _empty_extraction()

    for key in result:
        value = data.get(key, [])

        if isinstance(value, list):
            result[key] = value

    return result


# ============================================================================
# USER PROMPT
# ============================================================================

def _build_user_prompt(artifact: tuple) -> str:
    """
    Build the extraction prompt from the Database tuple returned by
    get_pending_artifacts().

    Tuple layout is:

        id,
        source_id,
        original_title,
        original_url,
        original_content,
        processing_status
    """
    artifact_id = artifact[0]
    original_title = artifact[2]
    original_url = artifact[3]
    original_content = artifact[4]

    content = original_content or ""

    if len(content) > MAX_CONTENT_CHARS:
        content = content[:MAX_CONTENT_CHARS]

    return f"""
SOURCE ARTIFACT ID: {artifact_id}

TITLE:
{original_title or "N/A"}

URL:
{original_url or "N/A"}

DOCUMENT:
{content}

Extract the claims, measurements, and entities explicitly supported by this
document.

Return JSON only.
"""


# ============================================================================
# LLM CALL
# ============================================================================

def _extract_all_llm(artifact: tuple) -> dict[str, list]:
    """
    Call the configured OpenAI-compatible router and return structured
    extraction results. Resilient: walks healthy routes (route_health ledger)
    instead of grinding a dead one.
    """
    from src.route_health import call_llm_resilient
    cfg = _get_router_config()

    if not cfg["api_key"]:
        raise RuntimeError("ROUTER_API_KEY is not configured.")

    prompt_user = _build_user_prompt(artifact)
    raw_text, used_route = call_llm_resilient(
        _build_router_client,
        [{"role": "system", "content": _SYSTEM_PROMPT},
         {"role": "user", "content": prompt_user}],
        _route_candidates(),
        temperature=TEMPERATURE,
        timeout=float(os.environ.get("ROUTER_CALL_TIMEOUT", "300")),
        max_tokens=MAX_TOKENS,
    )

    try:
        return _parse_llm_json(raw_text)
    except ValueError:
        # S3d repair pass: ask the model to reformat its own output as pure JSON.
        repair_text, _r2 = call_llm_resilient(
            _build_router_client,
            [
                {"role": "system",
                 "content": "You output ONLY valid minified JSON. No prose, no fences."},
                {"role": "user",
                 "content":
                     "Convert this to valid JSON matching the original schema. "
                     "Drop nothing; fix syntax only:\n\n" + raw_text[:12000]},
            ],
            _route_candidates(),
            temperature=0.0,
            timeout=float(os.environ.get("ROUTER_CALL_TIMEOUT", "300")),
            max_tokens=MAX_TOKENS,
        )
        repaired = repair_text
        try:
            return _parse_llm_json(repaired)
        except ValueError:
            # last resort: original strict parse error for the caller's log
            return _parse_llm_json(raw_text)


# ============================================================================
# RESULT TYPE
# ============================================================================

@dataclass
class Gate2Result:
    artifacts_processed: int = 0

    claims_extracted: int = 0
    measurements_extracted: int = 0
    entities_extracted: int = 0
    problems_extracted: int = 0

    errors: int = 0

    skipped_no_key: int = 0

    claim_ids: list[int] = field(default_factory=list)
    measurement_ids: list[int] = field(default_factory=list)
    entity_ids: list[int] = field(default_factory=list)
    problem_ids: list[int] = field(default_factory=list)


# ============================================================================
# ENTITY HANDLING
# ============================================================================

def _find_existing_entity_id(
    db: Database,
    name: str,
) -> Optional[int]:
    """
    Perform only exact entity matching in Gate 2.

    This is deliberately NOT the full Gate 3 resolution mechanism.

    Gate 3 will later handle:
      - aliases
      - fuzzy matching
      - embeddings
      - confidence
      - ambiguous candidates
    """
    name = _clean_string(name)

    if not name:
        return None

    entity = db.get_entity_by_name(name)

    if entity is None:
        return None

    return entity.id


def _insert_or_reuse_entity(
    db: Database,
    entity_data: dict[str, Any],
) -> Optional[int]:
    """
    Insert a new entity candidate unless an exact canonical entity already
    exists.

    Returns the entity ID.
    """
    name = _clean_string(entity_data.get("name"))

    if not name:
        return None

    # =====================================================================
    # ENTITY IDENTITY RESOLUTION — v0 (applied 2026-08-15)
    # =====================================================================
    # Exact-primary OR recorded-alias match: reuse the existing entity and
    # merge this mention's aliases into it, instead of creating a duplicate.
    # (Fixes the "aliases recorded but never resolved into one entity" gap
    # from the Entity audit.)
    #
    # Scope of v0 (documented, deferred):
    #   - Exact-equivalence ONLY: matches when the incoming name equals an
    #     existing primary name OR a recorded alias (casefold). No fuzzy
    #     matching, no embeddings, no phonetic/spelling normalization.
    #   - Therefore the SPELLING-VARIANT class (e.g. "Araghchi" vs "Araqchi",
    #     "Shehbaz Sharif" vs "Muhammad Shehbaz Sharif") is NOT merged, because
    #     the LLM did not record them as aliases of each other. That class
    #     needs either LLM spelling-normalization (prompt) or fuzzy matching
    #     (Gate 3) — a separate, larger change, intentionally out of scope
    #     here to avoid over-merging false positives.
    #   - Idempotent in either insertion order.
    existing = db.get_entity_by_name_or_alias(name)
    if existing is not None:
        for alias in _clean_aliases(entity_data.get("aliases")):
            db.add_alias_to_entity(existing.id, alias)
        return existing.id

    existing_id = _find_existing_entity_id(db, name)

    if existing_id is not None:
        return existing_id

    entity_type = _normalize_entity_type(
        entity_data.get("entity_type")
    )

    aliases = _clean_aliases(entity_data.get("aliases"))

    description = _clean_string(
        entity_data.get("description")
    )

    entity_id = db.insert_entity(
        name=name,
        ent_type=entity_type,
        aliases=aliases,
        description=description,
        relationships=None,
        importance=None,
        confidence=0.0,
    )

    return entity_id


# ============================================================================
# MEASUREMENT SUBJECT RESOLUTION
# ============================================================================

def _resolve_measurement_subject(
    subject_name: Any,
    local_entities: dict[str, int],
    db: Database,
) -> Optional[int]:
    """
    Resolve a measurement subject conservatively.

    Priority:
      1. Exact match against entities extracted from this artifact.
      2. Exact match against an existing global Entity.
      3. None.

    Fuzzy/semantic resolution is intentionally deferred to Gate 3.
    """
    subject = _clean_string(subject_name)

    if not subject:
        return None

    # Same-artifact entities take priority.
    local_id = local_entities.get(subject.casefold())

    if local_id is not None:
        return local_id

    # Existing global exact match.
    return _find_existing_entity_id(db, subject)


# ============================================================================
# CLAIM INSERTION
# ============================================================================

def _insert_claims(
    db: Database,
    artifact_id: int,
    claims: list[Any],
    result: Gate2Result,
) -> None:
    """Normalize and insert source claims."""
    for raw_claim in claims:
        if not isinstance(raw_claim, dict):
            continue

        proposition = _clean_string(
            raw_claim.get("proposition")
        )

        if not proposition:
            continue

        claim_type = _normalize_claim_type(
            raw_claim.get("claim_type")
        )

        author_source = _clean_string(
            raw_claim.get("author_source")
        )

        temporal_scope = _normalize_temporal_scope(
            raw_claim.get("temporal_scope")
        )

        claim_time = _parse_datetime(
            raw_claim.get("time")
        )

        claim_id = db.insert_claim(
            artifact_id=artifact_id,
            proposition=proposition,
            claim_type=claim_type,
            author_source=author_source,
            temporal_scope=temporal_scope,
            claim_origin="source",
            time=claim_time,
            supporting_info=[],
            contradicting_info=[],
            status="candidate",
            processing_history=[],
        )

        result.claim_ids.append(claim_id)
        result.claims_extracted += 1


# ============================================================================
# ENTITY INSERTION
# ============================================================================

def _insert_entities(
    db: Database,
    artifact_id: int,
    entities: list[Any],
    result: Gate2Result,
) -> dict[str, int]:
    """
    Insert/reuse entities and create artifact-entity provenance links.

    Returns:
        casefolded canonical entity name -> database ID
    """
    local_entities: dict[str, int] = {}

    for raw_entity in entities:
        if not isinstance(raw_entity, dict):
            continue

        entity_id = _insert_or_reuse_entity(
            db,
            raw_entity,
        )

        if entity_id is None:
            continue

        name = _clean_string(raw_entity.get("name"))

        if not name:
            continue

        # Provenance relation:
        # this artifact explicitly mentioned this entity.
        db.link_artifact_entity(
            artifact_id=artifact_id,
            entity_id=entity_id,
            confidence=1.0,
        )

        local_entities[name.casefold()] = entity_id

        if entity_id not in result.entity_ids:
            result.entity_ids.append(entity_id)
            result.entities_extracted += 1

    return local_entities


# ============================================================================
# MEASUREMENT INSERTION
# ============================================================================

def _insert_measurements(
    db: Database,
    artifact_id: int,
    measurements: list[Any],
    local_entities: dict[str, int],
    result: Gate2Result,
) -> None:
    """
    NO-OP since ontology v1.8.

    Gate 2 extracts from news prose. Under the v1.8 boundary, a Measurement is
    an authoritative quantitative-data layer obtained from a DESIGNATED
    measurement-grade source (central bank, statistics agency, approved API),
    NOT from general news articles. Quantitative information found in a news
    article is retained as a (quantitative) Claim and stays a Claim; it is NOT
    promoted to a Measurement.

    A dedicated measurement extractor for grade sources can be added later and
    should call db.insert_measurement(..., measurement_source=...,
    acquisition_method=...) directly. Until then, Gate 2 creates no
    Measurements.
    """
    return


def _insert_problem_signals(
    db: Database,
    artifact_id: int,
    problem_signals: list[Any],
    local_entities: dict[str, int],
    result: Gate2Result,
) -> None:
    """
    Insert structured Problem signals (Discovery Layer v1.9).

    Each signal: statement, polarity, topic, affected_entities (names already
    extracted this article), investigation_questions. Affected entities are
    linked only when they resolve to an entity extracted from THIS article
    (exact name match via local_entities). Evidence = the artifact itself.
    """
    name_to_id = {name.casefold(): eid for name, eid in local_entities.items()}

    for sig in problem_signals:
        if not isinstance(sig, dict):
            continue

        statement = _clean_string(sig.get("statement"))
        if not statement:
            continue

        polarity = _clean_string(sig.get("polarity")) or "problem"
        if polarity not in ("problem", "opportunity"):
            polarity = "problem"

        topic_name = _clean_string(sig.get("topic"))
        topic_id = db.upsert_topic(topic_name) if topic_name else None

        # Evidence: the originating artifact (reused-reference, per spec).
        problem_id = db.insert_problem(
            statement=statement,
            polarity=polarity,
            topic_id=topic_id,
            artifact_id=artifact_id,
            status="candidate",
        )

        # Step 48 -- Iran-compatibility gate: assess + persist verdict so the
        # Iran-scoped pipeline can route non-Iran problems out explicitly.
        try:
            iran = classify_problem_iran(
                statement,
                topic=topic_name,
                affected_entities=sig.get("affected_entities") or [],
            )
            db.set_problem_iran(problem_id, iran["iran_relevant"], iran["iran_note"])
        except Exception:
            # Never block problem insertion on the gate; default to pending.
            pass

        # Affected entities — only those extracted from this article.
        for ent_name in sig.get("affected_entities", []) or []:
            nm = _clean_string(ent_name)
            if not nm:
                continue
            eid = name_to_id.get(nm.casefold())
            if eid is not None:
                db.link_problem_entity(problem_id, eid)

        # Evidence reference to the artifact.
        db.link_problem_evidence(problem_id, "artifact", artifact_id)

        # Investigation questions.
        for rank, q in enumerate(sig.get("investigation_questions", []) or [], start=1):
            qtext = _clean_string(q)
            if qtext:
                db.insert_investigation_question(problem_id, qtext, rank)

        result.problem_ids.append(problem_id)
        result.problems_extracted += 1


# ============================================================================
# MAIN GATE 2
# ============================================================================

def run_gate2(
    db: Database,
    batch_size: int = BATCH_SIZE,
) -> Gate2Result:
    """
    Process pending SourceArtifacts through Gate 2.

    Important status semantics:

        pending
            -> extraction attempted

        successful extraction
            -> extracted

        failed extraction
            -> remains pending

    Gate 2 does NOT mark artifacts as 'accepted' merely because an API key
    is missing. This prevents the database from claiming that extraction
    succeeded when it did not.
    """
    result = Gate2Result()

    cfg = _get_router_config()

    if not cfg["api_key"]:
        print(
            "\n[Gate 2] ROUTER_API_KEY is not configured. "
            "No artifacts processed; pending artifacts remain pending."
        )
        result.skipped_no_key = len(
            db.get_pending_artifacts(limit=batch_size)
        )
        return result

    artifacts = db.get_pending_artifacts(
        limit=batch_size
    )

    if not artifacts:
        print("\n[Gate 2] No pending artifacts.")
        return result

    print(
        f"\n[Gate 2] Processing {len(artifacts)} pending artifact(s)"
    )
    print(
        f"         Model: {cfg['model']}"
    )

    for artifact in artifacts:
        artifact_id = artifact[0]

        print(
            f"\n  [Artifact {artifact_id}] "
            f"{(artifact[2] or '')[:70]}"
        )

        try:
            extraction = _extract_all_llm(artifact)

            claims = extraction.get("claims", [])
            measurements = extraction.get("measurements", [])
            entities = extraction.get("entities", [])
            problem_signals = extraction.get("problem_signals", [])

            # ------------------------------------------------------------
            # 1. Entities first
            #
            # Measurements may refer to entities by name. Therefore entity
            # candidates must exist before we attempt exact subject linking.
            # ------------------------------------------------------------

            local_entities = _insert_entities(
                db=db,
                artifact_id=artifact_id,
                entities=entities,
                result=result,
            )

            # ------------------------------------------------------------
            # 2. Claims
            # ------------------------------------------------------------

            _insert_claims(
                db=db,
                artifact_id=artifact_id,
                claims=claims,
                result=result,
            )

            # ------------------------------------------------------------
            # 3. Measurements
            # ------------------------------------------------------------

            _insert_measurements(
                db=db,
                artifact_id=artifact_id,
                measurements=measurements,
                local_entities=local_entities,
                result=result,
            )

            # ------------------------------------------------------------
            # 3b. Problem signals (Discovery Layer v1.9)
            # ------------------------------------------------------------

            _insert_problem_signals(
                db=db,
                artifact_id=artifact_id,
                problem_signals=problem_signals,
                local_entities=local_entities,
                result=result,
            )

            # ------------------------------------------------------------
            # 4. Only now mark the artifact as extracted.
            # ------------------------------------------------------------

            db.update_artifact_status(
                artifact_id,
                ProcessingStatus.EXTRACTED.value,
            )

            result.artifacts_processed += 1

            print(
                f"      claims={len(claims)}, "
                f"measurements={len(measurements)}, "
                f"entities={len(entities)}, "
                f"problems={len(problem_signals)}"
            )

            print("      -> extracted")

        except Exception as exc:
            result.errors += 1

            print(
                f"      [ERROR] Gate 2 failed for artifact "
                f"{artifact_id}: {exc}"
            )

            # Deliberately leave the artifact PENDING.
            # It can therefore be retried after the problem is fixed.

    print(
        f"\n[Gate 2] Done: "
        f"{result.artifacts_processed} processed, "
        f"{result.claims_extracted} claims, "
        f"{result.measurements_extracted} measurements, "
        f"{result.entities_extracted} entities, "
        f"{result.errors} errors"
    )

    return result


# ============================================================================
# DIRECT EXECUTION
# ============================================================================

if __name__ == "__main__":
    print(
        "Gate 2 is normally invoked through run_phase1.py "
        "with an initialized Database."
    )
