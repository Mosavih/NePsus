"""
Nexus-Think-Tank — Source configuration loader.

Reads `sources.yaml` from the project root, converts each entry into a
Source model, and ensures every source exists in the database (insert if
new, reuse if already present by name).

For Phase 1 this keeps the source list in a simple YAML file that can be
edited by hand. Later phases may move this to a database-managed config
or an API, but the contract (YAML → list[Source]) stays the same.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import Source, SourceType
from .database import Database


def _load_yaml(path: Path) -> list[dict[str, Any]]:
    """Read the YAML file and return the 'sources' list."""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not data or "sources" not in data:
        return []
    return data["sources"]


def _entry_to_source(entry: dict[str, Any]) -> Source:
    """Convert one YAML dict into a Source model."""
    # reliability_profile: accept either a simple tier int or a full dict
    reliability_raw = entry.get("reliability_tier") or entry.get("reliability_profile")
    if isinstance(reliability_raw, int):
        reliability_profile: dict[str, Any] = {"tier": reliability_raw}
    elif isinstance(reliability_raw, dict):
        reliability_profile = reliability_raw
    else:
        reliability_profile = {"tier": 3}

    return Source(
        name=entry["name"],
        type=SourceType(entry.get("type", "other")),
        language=entry.get("language", "en"),
        country=entry.get("country"),
        url=entry.get("url"),
        reliability_profile=reliability_profile,
        political_affiliation=entry.get("political_affiliation"),
        collection_method=entry.get("collection_method", "rss"),
        update_frequency=entry.get("update_frequency"),
        status=entry.get("status", "active"),
    )


def load_sources(config_path: Path, db: Database) -> list[Source]:
    """
    Load all sources from YAML, return Source objects that are guaranteed
    to have a database ID (existing or newly inserted).
    """
    entries = _load_yaml(config_path)
    sources: list[Source] = []

    for entry in entries:
        source = _entry_to_source(entry)

        # Check if the source already exists by name
        existing = db.get_source_by_name(source.name)
        if existing is not None:
            sources.append(existing)
            print(f"  [source] '{source.name}' already in DB (id={existing.id})")
        else:
            sid = db.insert_source(source)
            source.id = sid
            sources.append(source)
            print(f"  [source] '{source.name}' inserted (id={sid})")

    return sources
