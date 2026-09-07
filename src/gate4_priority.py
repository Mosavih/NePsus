"""
Nexus-Think-Tank — Gate 4: Priority

Gate 4 determines which extracted material deserves analytical attention.

Design principles:
  - Gate 4 does NOT extract, resolve, or synthesize information.
  - Gate 4 does NOT modify Claims, Entities, Measurements, or Events.
  - Gate 4 is deterministic in v0.
  - No LLM is required.
  - Priority is based on:
        1. source reliability
        2. novelty
        3. analytical density
  - The result is returned to the caller for Gate 5 routing.

The current database has no dedicated priority field, so Gate 4 does not
persist a score yet. This is intentional: we should validate the scoring
logic before changing the ontology/database contract.

================================================================================
FROZEN AS v0 (post pin-corpus experiment)
================================================================================
Gate 4 is frozen at this implementation. The experiment (run against a pinned
corpus of 10 real articles + 1 clearly-labeled synthetic soft-news item)
validated the original architectural goal: a cheap, deterministic priority
mechanism that separates high-value material from soft-news without an LLM.

What is frozen:
  - Gate 4 is a DETERMINISTIC PRIORITY / ATTENTION ranking, NOT a truth or
    confidence assessment. It does not decide whether an article is true,
    whether an event exists, or whether an entity is valid. Those questions
    belong to other gates (Gate 3 / 3.5 / measurement resolution).
  - The scoring function and thresholds are unchanged from this v0 form.

Known v0 limitations (documented, not yet fixed — by design):
  - RELIABILITY is uninformative while only one source exists: with a single
    source every artifact scores the same reliability, so it contributes a
    constant floor and no differentiation. It may become informative once
    multiple sources with distinct reliability tiers are present.
  - NOVELTY requires stress testing on a mature / larger KB. In a young KB
    almost every entity is "novel", so novelty currently measures knowledge-
    base immaturity rather than article information value, and trends toward
    saturation on small single-source corpora.
  - The WEIGHTS (0.35 / 0.35 / 0.30) are empirical v0 parameters, NOT
    validated universal weights. They can be recalibrated later from human
    review or Gate 6 outcomes.

Future evaluation (not part of v0):
  - Test Gate 4 on MULTI-SOURCE and LARGER-CORPUS conditions, where novelty and
    reliability behave differently than on the single-source pin corpus.
  - Revisit the weight balance only after those conditions are observed.

The architecture explicitly favors plain Python + SQLite here. No LLM call is
added. Gate 4 stays cheap and deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .database import Database


# ============================================================================
# Configuration
# ============================================================================

# Score weights.
#
# These are deliberately simple and transparent for v0.
# They can be calibrated later from human review / Gate 6 outcomes.

WEIGHT_RELIABILITY = 0.35
WEIGHT_NOVELTY = 0.35
WEIGHT_ANALYTICAL_DENSITY = 0.30


# Priority thresholds.
PRIORITY_HIGH = 0.70
PRIORITY_MEDIUM = 0.40


# ============================================================================
# Result types
# ============================================================================

@dataclass
class PriorityDecision:
    artifact_id: int

    score: float
    priority: str

    reliability_score: float
    novelty_score: float
    analytical_density_score: float

    claim_count: int
    measurement_count: int
    entity_count: int

    novel_entity_count: int

    reason: str


@dataclass
class Gate4Result:
    artifacts_evaluated: int = 0
    high_priority: int = 0
    medium_priority: int = 0
    low_priority: int = 0

    decisions: list[PriorityDecision] | None = None

    def __post_init__(self) -> None:
        if self.decisions is None:
            self.decisions = []


# ============================================================================
# Source reliability
# ============================================================================

def _reliability_score(reliability_profile: object) -> float:
    """
    Convert the current reliability_profile into a normalized [0, 1] score.

    Current sources.yaml convention:
        {"tier": 1}
        {"tier": 2}
        ...

    Higher tier number currently means lower reliability.

    If the profile is missing or malformed, return a conservative midpoint.
    """

    if not isinstance(reliability_profile, dict):
        return 0.5

    tier = reliability_profile.get("tier")

    try:
        tier = float(tier)
    except (TypeError, ValueError):
        return 0.5

    # v0 convention:
    # tier 1 = highest reliability
    # tier 5+ = lowest
    #
    # Clamp rather than allowing pathological values.
    tier = max(1.0, min(tier, 5.0))

    return 1.0 - ((tier - 1.0) / 4.0)


# ============================================================================
# Novelty
# ============================================================================

def _novelty_score(
    db: Database,
    artifact_id: int,
) -> tuple[float, int, int]:
    """
    Estimate novelty from artifact-specific entities.

    An entity occurring in only this artifact is stronger evidence of novelty
    than an entity repeatedly appearing across the corpus.

    Returns:
        (novelty_score, novel_entity_count, total_entity_count)
    """

    artifact_entities = db.get_artifact_entities(artifact_id)

    if not artifact_entities:
        return 0.0, 0, 0

    total = len(artifact_entities)
    novel = 0

    for entity_id, _confidence in artifact_entities:
        row = db.conn.execute(
            """
            SELECT COUNT(DISTINCT artifact_id)
            FROM artifact_entities
            WHERE entity_id = ?
            """,
            (entity_id,),
        ).fetchone()

        artifact_count = int(row[0]) if row else 0

        if artifact_count <= 1:
            novel += 1

    return novel / total, novel, total


# ============================================================================
# Analytical density
# ============================================================================

def _analytical_density(
    claim_count: int,
    measurement_count: int,
    entity_count: int,
) -> float:
    """
    Estimate how analytically information-dense an artifact is.

    Measurements receive the strongest signal because they provide
    structured quantitative evidence.

    Claims provide the main analytical substance.

    Entities contribute weakly: entity count alone must NOT make an article
    important, otherwise entity-rich soft news would be over-prioritized.
    """

    claim_signal = min(claim_count / 10.0, 1.0)
    measurement_signal = min(measurement_count / 3.0, 1.0)
    entity_signal = min(entity_count / 10.0, 1.0)

    score = (
        0.50 * claim_signal
        + 0.40 * measurement_signal
        + 0.10 * entity_signal
    )

    return min(max(score, 0.0), 1.0)


# ============================================================================
# Priority classification
# ============================================================================

def _classify_priority(score: float) -> str:
    if score >= PRIORITY_HIGH:
        return "high"

    if score >= PRIORITY_MEDIUM:
        return "medium"

    return "low"


def _build_reason(
    priority: str,
    reliability: float,
    novelty: float,
    analytical_density: float,
) -> str:
    signals = []

    if reliability >= 0.75:
        signals.append("high source reliability")
    elif reliability < 0.40:
        signals.append("low source reliability")

    if novelty >= 0.70:
        signals.append("high novelty")
    elif novelty < 0.30:
        signals.append("low novelty")

    if analytical_density >= 0.70:
        signals.append("high analytical density")
    elif analytical_density < 0.30:
        signals.append("low analytical density")

    if not signals:
        signals.append("mixed priority signals")

    return f"{priority} priority: " + ", ".join(signals)


# ============================================================================
# Single artifact evaluation
# ============================================================================

def evaluate_artifact(
    db: Database,
    artifact_id: int,
) -> PriorityDecision:
    """
    Calculate the Gate 4 priority decision for one extracted artifact.

    The artifact must already have passed Gate 2 and Gate 3.
    """

    row = db.conn.execute(
        """
        SELECT
            sa.id,
            sa.source_id,
            sa.processing_status
        FROM source_artifacts sa
        WHERE sa.id = ?
        """,
        (artifact_id,),
    ).fetchone()

    if row is None:
        raise ValueError(f"Artifact {artifact_id} does not exist.")

    # ------------------------------------------------------------------
    # Source reliability
    # ------------------------------------------------------------------

    source = db.conn.execute(
        """
        SELECT reliability_profile
        FROM sources
        WHERE id = ?
        """,
        (row["source_id"],),
    ).fetchone()

    if source is None:
        reliability = 0.5
    else:
        import json

        try:
            profile = json.loads(source["reliability_profile"] or "{}")
        except (TypeError, ValueError):
            profile = {}

        reliability = _reliability_score(profile)

    # ------------------------------------------------------------------
    # Corpus counts
    # ------------------------------------------------------------------

    claim_count = int(
        db.conn.execute(
            "SELECT COUNT(*) FROM claims WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()[0]
    )

    measurement_count = int(
        db.conn.execute(
            "SELECT COUNT(*) FROM measurements WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()[0]
    )

    entity_count = int(
        db.conn.execute(
            """
            SELECT COUNT(*)
            FROM artifact_entities
            WHERE artifact_id = ?
            """,
            (artifact_id,),
        ).fetchone()[0]
    )

    # ------------------------------------------------------------------
    # Novelty
    # ------------------------------------------------------------------

    novelty, novel_entity_count, _ = _novelty_score(
        db,
        artifact_id,
    )

    # ------------------------------------------------------------------
    # Analytical density
    # ------------------------------------------------------------------

    density = _analytical_density(
        claim_count=claim_count,
        measurement_count=measurement_count,
        entity_count=entity_count,
    )

    # ------------------------------------------------------------------
    # Final score
    # ------------------------------------------------------------------

    score = (
        WEIGHT_RELIABILITY * reliability
        + WEIGHT_NOVELTY * novelty
        + WEIGHT_ANALYTICAL_DENSITY * density
    )

    score = round(min(max(score, 0.0), 1.0), 4)

    priority = _classify_priority(score)

    reason = _build_reason(
        priority,
        reliability,
        novelty,
        density,
    )

    return PriorityDecision(
        artifact_id=artifact_id,
        score=score,
        priority=priority,
        reliability_score=round(reliability, 4),
        novelty_score=round(novelty, 4),
        analytical_density_score=round(density, 4),
        claim_count=claim_count,
        measurement_count=measurement_count,
        entity_count=entity_count,
        novel_entity_count=novel_entity_count,
        reason=reason,
    )


# ============================================================================
# Gate 4
# ============================================================================

def run_gate4(
    db: Database,
    artifact_ids: Optional[list[int]] = None,
) -> Gate4Result:
    """
    Run Gate 4 on extracted artifacts.

    If artifact_ids is omitted, all artifacts that have completed Gate 2
    are evaluated.

    Gate 4 does NOT modify the database.
    """

    result = Gate4Result()

    if artifact_ids is None:
        rows = db.conn.execute(
            """
            SELECT id
            FROM source_artifacts
            WHERE processing_status = 'extracted'
            ORDER BY id
            """
        ).fetchall()

        artifact_ids = [int(row["id"]) for row in rows]

    for artifact_id in artifact_ids:
        try:
            decision = evaluate_artifact(
                db,
                artifact_id,
            )

            result.decisions.append(decision)
            result.artifacts_evaluated += 1

            if decision.priority == "high":
                result.high_priority += 1
            elif decision.priority == "medium":
                result.medium_priority += 1
            else:
                result.low_priority += 1

        except Exception as exc:
            print(
                f"[Gate 4] Error evaluating artifact "
                f"{artifact_id}: {exc}"
            )

    # Highest priority first.
    result.decisions.sort(
        key=lambda d: d.score,
        reverse=True,
    )

    print(
        f"[Gate 4] Evaluated {result.artifacts_evaluated} artifact(s): "
        f"{result.high_priority} high, "
        f"{result.medium_priority} medium, "
        f"{result.low_priority} low."
    )

    return result


# ============================================================================
# CLI
# ============================================================================

if __name__ == "__main__":
    from .database import Database

    db = Database()
    db.init()

    result = run_gate4(db)

    print("\nGate 4 priorities:")
    print("-" * 80)

    for decision in result.decisions:
        print(
            f"Artifact {decision.artifact_id:>3} | "
            f"{decision.priority:<6} | "
            f"score={decision.score:.3f} | "
            f"claims={decision.claim_count:<3} | "
            f"measurements={decision.measurement_count:<2} | "
            f"entities={decision.entity_count:<2} | "
            f"novel={decision.novel_entity_count:<2}"
        )
        print(f"    {decision.reason}")

    db.close()