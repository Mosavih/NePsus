"""
Nexus-Think-Tank — Pydantic models for Section 3.1 core objects.

These models are the stable schema contract for the entire system. Every
gate, agent, and storage layer speaks in terms of these types. They are
designed to be extended, never rewritten — new fields are additive.

Ontology notes (reconciled pass):
  - There is NO dedicated "Evidence" or "Knowledge" object. The word
    "evidence" in the spec refers to SourceArtifacts that document an
    Event; that link is stored in the `event_evidence` junction table.
  - "Evidence IDs" in the Event spec = artifact IDs, not a separate
    object type. Similarly "Entity IDs" = links via `event_entities`.
    Neither is a scalar field on Event; both are relations.
  - Claim preserves provenance via `artifact_id` (which artifact it was
    extracted from) and distinguishes source claims from analytical
    claims via `claim_origin`.
  - Measurement is a first-class core object. Its initial schema is
    deliberately small: indicator + subject + value + unit + reference
    period + provenance + status.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Return timezone-aware UTC now, suitable as a Pydantic default_factory."""
    return datetime.now(timezone.utc)


# ── Enums ──────────────────────────────────────────────────────────────

class SourceType(str, Enum):
    NEWS_AGENCY = "news_agency"
    GOVERNMENT = "government"
    SOCIAL_MEDIA = "social_media"
    DATABASE = "database"
    RESEARCH = "research"
    OTHER = "other"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ARCHIVED = "archived"
    EXTRACTED = "extracted"


class ClaimOrigin(str, Enum):
    SOURCE = "source"           # extracted from a SourceArtifact
    ANALYTICAL = "analytical"   # produced by an agent or human analyst


class ClaimStatus(str, Enum):
    CANDIDATE = "candidate"
    ACCEPTED = "accepted"
    CONTESTED = "contested"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class MeasurementStatus(str, Enum):
    RAW = "raw"
    VERIFIED = "verified"
    REVISED = "revised"
    DISCARDED = "discarded"


# ── Section 3.1.1 — Source ─────────────────────────────────────────────

class Source(BaseModel):
    """
    Describes WHERE information originates — the provider, not the content.
    Maps to the `sources` table in SQLite.
    """
    id: Optional[int] = None
    name: str = Field(..., description="Human-readable name, e.g. 'Reuters', 'ISNA'")
    type: SourceType = Field(SourceType.OTHER)
    language: str = Field("en", description="ISO 639-1 language code")
    country: Optional[str] = Field(None, description="ISO 3166-1 alpha-2 country code")
    url: Optional[str] = Field(None, description="Canonical homepage or feed URL")
    reliability_profile: dict[str, Any] = Field(
        default_factory=lambda: {"tier": 3},
        description=(
            "Reliability profile. Starts as {'tier': 1-5} but may grow "
            "context-dependent dimensions (e.g. per-domain scores). "
            "1=high trust (established wire services), 5=low (unverified)."
        ),
    )
    political_affiliation: Optional[str] = None
    collection_method: str = Field("rss", description="rss | api | scraping")
    update_frequency: Optional[str] = Field(None, description="Human-readable, e.g. 'hourly'")
    status: str = Field("active", description="active | inactive")
    created_at: datetime = Field(default_factory=_utcnow)

    model_config = {
        "use_enum_values": True,
        "extra": "ignore",
    }


# ── Section 3.1.2 — Source Artifact ────────────────────────────────────

class SourceArtifact(BaseModel):
    """
    A specific piece of information retrieved from a Source.
    Maps to the `source_artifacts` table in SQLite.
    This is what Gate 1 produces and Gate 2 consumes.
    A Source Artifact may contain one or more Claims or Measurements
    once Gate 2 has processed it.
    """
    id: Optional[int] = None
    source_id: int = Field(..., description="FK to sources.id")
    original_title: Optional[str] = None
    original_url: Optional[str] = None
    original_content: str = Field(..., description="Cleaned main-text from trafilatura")
    language: Optional[str] = None
    media_type: str = Field("text", description="text | image | video | audio | dataset")
    content_hash: str = Field(
        ..., description="SHA-256 of normalized original_content, for dedup",
    )
    publication_time: Optional[datetime] = None
    collection_time: datetime = Field(default_factory=_utcnow)
    processing_status: ProcessingStatus = Field(ProcessingStatus.PENDING)

    model_config = {
        "use_enum_values": True,
        "extra": "ignore",
    }


# ── Section 3.1.3 — Event ──────────────────────────────────────────────

class Event(BaseModel):
    """
    A discrete occurrence in the world that has occurred or is occurring.

    "Evidence IDs" and "Entity IDs" from the spec are NOT scalar fields
    here — they are relations backed by junction tables (`event_evidence`
    links to source_artifacts, `event_entities` links to entities).
    The Event model represents only intrinsic properties of the occurrence.

    Temporal rule: future occurrences are Claims or Forecasts, not Events.
    The Event represents the occurrence itself; uncertainty is derived
    from available supporting/contradicting information, not stored as
    an intrinsic property.
    """
    id: Optional[int] = None
    title: str
    summary: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    importance: Optional[str] = None
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    category: Optional[str] = None
    status: str = "ongoing"


# ── Section 3.1.4 — Entity ──────────────────────────────────────────────

class Entity(BaseModel):
    """
    A persistent identifiable thing relevant to the system.

    Relationships between entities are not a universal first-class object
    (per spec). The `relationships` field is a JSON-serializable list of
    relationship descriptors, populated when relevant within Events,
    Claims, or Analyses. Explicit relationship modeling can be introduced
    later if a concrete computational requirement justifies it.
    """
    id: Optional[int] = None
    name: str
    entity_type: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    description: Optional[str] = None
    relationships: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Deferred — stored as JSON when populated",
    )
    importance: float = Field(
        0.0,
        ge=0.0,
        description="Numeric relevance score used for entity prioritization",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)


# ── Section 3.1.5 — Claim ───────────────────────────────────────────────

class Claim(BaseModel):
    """
    A proposition about the world expressed by a Source, analyst, or
    forecasting system.

    Provenance: `artifact_id` links to the SourceArtifact the claim was
    extracted from (for source claims). `claim_origin` distinguishes source
    claims from analytical claims. `time` is when the claim was made or
    issued. Supporting/contradicting information references other Claim IDs
    or Artifact IDs.

    A Claim may support, contradict, describe, or interpret an Event,
    Measurement, Entity, or another Claim.
    """
    id: Optional[int] = None
    artifact_id: Optional[int] = Field(
        None, description="FK to source_artifacts.id — provenance for source claims"
    )
    proposition: str
    claim_type: Optional[str] = Field(
        None,
        description="fact | statement | measurement | prediction | observation | causal | relationship",
    )
    claim_origin: ClaimOrigin = Field(
        ClaimOrigin.SOURCE,
        description="source (from artifact) | analytical (from agent/human)",
    )
    author_source: Optional[str] = Field(
        None, description="Who made the claim (person, organization, or 'system')"
    )
    time: Optional[datetime] = Field(
        None, description="When the claim was made or issued"
    )
    temporal_scope: Optional[str] = Field(
        None, description="past | present | future"
    )
    supporting_info: list[int] = Field(
        default_factory=list,
        description="IDs of supporting Claims or Artifacts",
    )
    contradicting_info: list[int] = Field(
        default_factory=list,
        description="IDs of contradicting Claims or Artifacts",
    )
    status: ClaimStatus = Field(ClaimStatus.CANDIDATE)
    processing_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Audit trail of processing steps (JSON)",
    )

    model_config = {
        "use_enum_values": True,
        "extra": "ignore",
    }


# ── Section 3.1.7 — Measurement ─────────────────────────────────────────

class Measurement(BaseModel):
    """
    A structured quantitative observation from a designated measurement-grade
    source or measurement system (ontology v1.8).

    A Measurement is distinguished from a Claim by its measurement provenance
    and acquisition pathway, NOT merely by the presence of a number. News
    articles may contain quantitative Claims, but those remain Claims.

    Schema:
      indicator + subject + value + unit + reference_period + provenance
      + measurement_source + acquisition_method + quality + status.
    """
    id: Optional[int] = None
    indicator: str = Field(
        ..., description="What was measured, e.g. 'inflation', 'USD/IRR exchange rate'"
    )
    subject_entity_id: Optional[int] = Field(
        None, description="FK to entities.id — which entity the measurement pertains to"
    )
    subject_entity_name: Optional[str] = Field(
        None,
        description=(
            "Normalized source mention retained until Gate 3 resolves it to "
            "subject_entity_id"
        ),
    )
    value: float
    unit: Optional[str] = Field(None, description="e.g. '%', 'IRR/kg', 'million barrels/day'")
    reference_period: Optional[str] = Field(
        None, description="Period the measurement covers, e.g. '2026-Q3' or '2026-08'"
    )
    measurement_time: Optional[datetime] = Field(
        None, description="When the measurement was taken or reported"
    )
    artifact_id: Optional[int] = Field(
        None,
        description=(
            "FK to source_artifacts.id — provenance. NULL when the Measurement "
            "originates from a designated measurement-grade source with no "
            "news artifact (ontology v1.8)."
        ),
    )
    measurement_source: Optional[str] = Field(
        None,
        description=(
            "Designated measurement-grade source (e.g. central bank, official "
            "statistics agency, approved API). Distinguishes authoritative "
            "measurements from news-derived Claims."
        ),
    )
    acquisition_method: Optional[str] = Field(
        None, description="How the value was obtained (approved pathway)."
    )
    quality: Optional[str] = Field(
        None,
        description=(
            "Quality / provenance notes. Distinct from value uncertainty: a "
            "measurement may have incomplete metadata while remaining valid."
        ),
    )
    collection_time: datetime = Field(default_factory=_utcnow)
    status: MeasurementStatus = Field(MeasurementStatus.RAW)

    model_config = {
        "use_enum_values": True,
        "extra": "ignore",
    }


# ── Section 3.1.8 — Analysis Record ────────────────────────────────────

class AnalysisRecord(BaseModel):
    """
    A structured analytical process conducted by a human or AI system.

    It answers: given the available information at this point in time, what
    did the analyst conclude, why, and with what alternatives and uncertainties?

    An Analysis Record must not overwrite Events or Source information.
    It represents an interpretation at a particular point in time.

    "Evidence used" = SourceArtifact IDs that the analysis drew upon.
    "Claims" = Claim IDs produced by this analysis.
    """
    id: Optional[int] = None
    question: str
    author: Optional[str] = Field(None, description="agent name or human")
    date: datetime = Field(default_factory=_utcnow)
    evidence_artifact_ids: list[int] = Field(
        default_factory=list,
        description="FKs to source_artifacts.id — the evidence drawn upon",
    )
    assumptions: list[str] = Field(default_factory=list)
    reasoning_chain: Optional[str] = None
    claim_ids: list[int] = Field(
        default_factory=list,
        description="Claim IDs produced by this analysis",
    )
    alternative_hypotheses: list[str] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    forecast_ids: list[int] = Field(
        default_factory=list,
        description="Forecast IDs derived from this analysis",
    )


# ── Section 3.1.9 — Forecast ───────────────────────────────────────────

class Forecast(BaseModel):
    """
    A probabilistic claim concerning a future outcome with an explicit
    resolution condition.

    A Forecast remains unresolved until its resolution condition is
    reached. Its eventual outcome is used to evaluate forecasting
    performance and improve future forecasting.
    """
    id: Optional[int] = None
    prediction: str
    probability: float = Field(0.5, ge=0.0, le=1.0)
    target_horizon: Optional[str] = Field(None, description="By when, e.g. '2027-Q4'")
    conditions: Optional[str] = None
    resolution_criteria: Optional[str] = None
    date_issued: datetime = Field(default_factory=_utcnow)
    author: Optional[str] = None
    supporting_analysis_id: Optional[int] = Field(
        None, description="FK to analyses.id — the analysis backing this forecast"
    )
    outcome: Optional[str] = None
    evaluation_status: str = "unresolved"
    evaluation_score: Optional[float] = None
