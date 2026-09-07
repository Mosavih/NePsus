"""
Nexus-Think-Tank - Gate 3.5: Event Synthesis and Evidence Linking

Gate 3.5 takes extracted artifacts and synthesizes discrete Event objects,
linking them to:
  1. event_evidence: the SourceArtifact that serves as evidence.
  2. event_entities: entities involved in the event.

Step 1 change (this file only):
  Before inserting an Event, an LLM eligibility gate decides whether the
  artifact describes a genuine discrete Event (per ontology section 3.1.3) or
  merely an article topic / opinion / future wish / analytical piece. Only
  artifacts judged to be real events produce an Event row.

The event<->entity relationship machinery is intentionally UNCHANGED: each
event links ONLY to the entities extracted from its own evidence artifact.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

from .database import Database
from .gate2_extraction import _get_router_config


# ============================================================================
# CONFIGURATION
# ============================================================================

EVENT_MAX_TOKENS = 256
EVENT_TEMPERATURE = 0.0


# ============================================================================
# EVENT ELIGIBILITY DECISION (LLM gate added in Step 1)
# ============================================================================

_EVENT_SYSTEM_PROMPT = """
You are the event-eligibility gate of an intelligence-analysis system.

The ontology (section 3.1.3) defines an EVENT as a DISCRETE OCCURRENCE in the
world that HAS OCCURRED or IS CURRENTLY OCCURRING. An event must describe
something that actually happened or is happening - not merely an idea, an
article topic, a policy opinion, a future wish, or an analytical commentary.

Examples that ARE events:
  - A ceasefire was reached on a specific date.
  - An exhibition opened, a summit was held, or an election was conducted.
  - A law was passed, a strike was launched, or an airstrike occurred.

Examples that are NOT events (do NOT synthesize an Event):
  - An opinion or policy recommendation ("production support should be accelerated").
  - An analytical article or editorial ("the document of America's defeat").
  - A description of a capability or platform ("a platform uses AI to teach language").
  - A future or aspirational wish ("a coach would love to face Iran").
  - A social pleasantry or generic gathering with no concrete occurrence.

Return exactly one JSON object:
{
  "is_event": true,
  "reason": "one short sentence explaining the decision"
}
"""

_EVENT_USER_TEMPLATE = """
ARTICLE TITLE:
{title}

ARTICLE PREVIEW:
{preview}

Does this article report a discrete, real-world occurrence that has happened or
is happening (an EVENT), or is it merely a topic, opinion, analysis, future wish,
or description of a capability? Answer with is_event and a brief reason.
"""


def _parse_eligibility_json(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if text.startswith("```"):
        # strip a leading ```json fence if present
        text = text.split("```", 2)[1] if "```" in text[3:] else text
        if text.startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return {"is_event": True}
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return {"is_event": True}
    if not isinstance(data, dict):
        return {"is_event": True}
    return data


def _decide_event_eligibility(title: str, preview: str) -> tuple[bool, str]:
    """Return (is_event, reason).

    Conservative by default: if the router is unconfigured, errors, or returns
    unparseable output, the artifact is treated as eligible (is_event=True) so
    the pipeline never silently drops an Event and the no-key path behaves as
    before. Only a confident is_event=false skips synthesis.
    """
    from src.investigation_layer.models import combo_for
    cfg = _get_router_config()
    if not cfg.get("api_key"):
        return True, "no router key configured; defaulting to eligible"

    try:
        from src.route_health import chat as _rchat_ev
        _text, _used = _rchat_ev("events",
            messages=[
                {"role": "system", "content": _EVENT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _EVENT_USER_TEMPLATE.format(
                        title=title or "Untitled",
                        preview=preview,
                    ),
                },
            ],
            max_tokens=EVENT_MAX_TOKENS,
            temperature=EVENT_TEMPERATURE,
            response_format={"type": "json_object"},
        )
        raw_text = _text or ""
        data = _parse_eligibility_json(raw_text)
        return bool(data.get("is_event", True)), str(data.get("reason", ""))
    except Exception:  # pragma: no cover - network / parse safety net
        return True, "eligibility call failed; defaulting to eligible"


# ============================================================================
# SYNTHESIS
# ============================================================================

@dataclass
class EventSynthesisResult:
    events_created: int = 0
    events_skipped: int = 0
    evidence_links_created: int = 0
    entity_links_created: int = 0


def synthesize_events_for_unlinked_artifacts(db: Database) -> EventSynthesisResult:
    """Find extracted artifacts with no event_evidence row; for each one the LLM
    judges to describe a genuine discrete Event, create an Event plus its
    evidence and entity links."""
    result = EventSynthesisResult()

    unlinked_artifacts = db.conn.execute(
        """SELECT sa.id, sa.original_title, sa.original_content, sa.publication_time
           FROM source_artifacts sa
           WHERE sa.processing_status = 'extracted'
             AND sa.id NOT IN (SELECT artifact_id FROM event_evidence)"""
    ).fetchall()

    if not unlinked_artifacts:
        return result

    for a in unlinked_artifacts:
        aid = a["id"]
        title = a["original_title"] or "Untitled Event"

        # Content preview for summary AND for the eligibility decision.
        content = a["original_content"] or ""
        preview = content[:600] + "..." if len(content) > 600 else content
        summary = content[:300] + "..." if len(content) > 300 else content

        # --- Step 1: LLM event-eligibility gate (does NOT touch linking) ---
        is_event, reason = _decide_event_eligibility(title, preview)
        if not is_event:
            result.events_skipped += 1
            print(
                f"[Gate 3.5] Skipped event for artifact {aid} "
                f"(not a discrete event): {title!r} - {reason}"
            )
            continue

        # Create Event
        eid = db.insert_event(
            title=title,
            summary=summary,
            location="Iran",
            importance="medium",
            confidence=0.8,
            category="general",
            status="ongoing",
        )
        result.events_created += 1

        # Link event_evidence (the artifact IS the evidence)
        db.link_event_evidence(event_id=eid, artifact_id=aid)
        result.evidence_links_created += 1

        # Link ONLY the entities actually extracted from this artifact.
        # get_artifact_entities returns (entity_id, confidence) tuples for the
        # artifact; these are the epistemically meaningful associations. Linking
        # every entity in the global table would make the event<->entity graph
        # worthless (the Phase 1.1 cross-product bug).
        for entity_id, confidence in db.get_artifact_entities(aid):
            db.link_event_entity(
                event_id=eid,
                entity_id=entity_id,
                confidence=confidence,
            )
            result.entity_links_created += 1

    return result
