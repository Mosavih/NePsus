"""Nexus-Think-Tank — Gate 3: Entity Resolution & Disambiguation.

Gate 3 operates only on structured source mentions retained by Gate 2.  It
performs deterministic resolution (canonical name, then recorded aliases).
Potential semantic/fuzzy matches deliberately remain for a future reviewed
resolver; automatically merging them would corrupt the knowledge base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .database import Database


@dataclass
class Gate3Result:
    entities_resolved: int = 0
    entities_created: int = 0
    aliases_added: int = 0
    measurements_linked: int = 0


def resolve_entity_name(
    db: Database,
    raw_name: str,
    entity_type: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    create: bool = True,
) -> tuple[Optional[int], bool, int]:
    """Resolve a source name, returning ``(entity_id, created, aliases_added)``.

    When ``create`` is False (used for measurement subjects), the resolver only
    links to an EXISTING entity found by exact name or recorded alias. If no
    such entity exists, it returns ``(None, False, 0)`` and leaves the caller to
    keep ``subject_entity_id`` NULL rather than inventing a spurious entity.

    Rationale: a NULL relationship is epistemically safer than a false one.
    Global/aggregate or category subjects (e.g. "state", "global goods") must
    not be turned into pseudo-entities. Fuzzy matching is deliberately NOT added
    here yet — unresolved subjects are retained as ``subject_entity_name`` for a
    future reviewed resolver.
    """
    name_clean = raw_name.strip()
    if not name_clean:
        raise ValueError("An entity name cannot be blank.")

    existing = db.get_entity_by_name(name_clean)
    if existing is None:
        existing = db.find_entity_by_alias(name_clean)

    if existing is not None:
        aliases_added = sum(
            db.add_alias_to_entity(existing.id, alias)
            for alias in aliases or []
            if alias.strip().casefold() != existing.name.casefold()
        )
        return existing.id, False, aliases_added

    if not create:
        # Do not invent an entity from an arbitrary measurement subject string.
        # Leave resolution to a human-reviewed / fuzzy resolver later.
        return None, False, 0

    entity_id = db.insert_entity(
        name=name_clean,
        ent_type=(entity_type or "unknown").strip() or "unknown",
        aliases=[alias.strip() for alias in aliases or [] if alias.strip()],
    )
    return entity_id, True, 0


def link_unresolved_measurements(db: Database, result: Gate3Result) -> None:
    """Resolve retained measurement subject mentions against EXISTING entities.

    Measurement subjects are linked only when they match an already-known entity
    by exact name or recorded alias. If no existing entity matches, the subject
    is left unresolved (subject_entity_id stays NULL) and the raw mention is
    retained in subject_entity_name for later human/fuzzy review. This prevents
    the resolver from inventing spurious pseudo-entities ("state", "global goods")
    from arbitrary measurement subject strings.
    """
    for measurement in db.get_unresolved_measurements():
        entity_id, created, aliases_added = resolve_entity_name(
            db, measurement["subject_entity_name"], create=False
        )
        if entity_id is None:
            # Epistemically correct: no relationship is better than a false one.
            result.entities_resolved += 0
            continue
        db.update_measurement_subject(measurement["id"], entity_id)
        result.measurements_linked += 1
        result.entities_resolved += 1
        result.entities_created += int(created)
        result.aliases_added += aliases_added


def run_gate3(db: Database) -> Gate3Result:
    """Resolve persisted, unresolved measurement subjects into entity records."""
    result = Gate3Result()
    link_unresolved_measurements(db, result)
    print(
        "[Gate 3] Resolution complete: "
        f"{result.measurements_linked} measurement(s) linked; "
        f"{result.entities_created} entity/entities created."
    )
    return result
