"""Tests for Nexus-Think-Tank Phase 1: Gate 1 + Gate 2 vertical slice."""

import os
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest

# Ensure we can import src modules
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import (
    Source, SourceArtifact, Claim,
    SourceType, ProcessingStatus, ClaimOrigin, ClaimStatus,
    MeasurementStatus,
)
from src.database import Database
from src.load_sources import load_sources
from src.gate1_collection import collect_from_source, _content_hash, _normalize_text
from src.gate2_extraction import run_gate2
from src.gate3_resolution import run_gate3


# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def temp_db():
    """Create a temporary database for each test."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    db = Database(db_path)
    db.init()
    yield db
    db.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def sample_source(temp_db):
    """Create a sample source in the database."""
    source = Source(
        name="Test Source",
        type=SourceType.NEWS_AGENCY,
        language="en",
        country="US",
        url="https://example.com/rss",
        reliability_profile={"tier": 2},
    )
    sid = temp_db.insert_source(source)
    source.id = sid
    return source


# ── Model Tests ────────────────────────────────────────────────────────

def test_source_model_validation():
    """Source model validates required fields."""
    s = Source(name="Test", url="https://example.com")
    assert s.name == "Test"
    assert s.type == SourceType.OTHER  # default
    assert s.language == "en"  # default
    assert s.reliability_profile == {"tier": 3}  # default


def test_source_artifact_validation():
    """SourceArtifact requires content_hash."""
    artifact = SourceArtifact(
        source_id=1,
        original_content="test content",
        content_hash="abc123",
    )
    assert artifact.content_hash == "abc123"
    assert artifact.processing_status == ProcessingStatus.PENDING


def test_claim_model_validation():
    """Claim model has correct defaults for provenance."""
    c = Claim(
        artifact_id=1,
        proposition="Sanctions are reducing imports.",
    )
    assert c.claim_origin == "source"
    assert c.status == "candidate"
    assert c.supporting_info == []
    assert c.contradicting_info == []
    assert c.processing_history == []


def test_measurement_model_validation():
    """Measurement is first-class with indicator + subject + value + unit."""
    from src.models import Measurement
    m = Measurement(
        indicator="inflation",
        value=42.5,
        unit="%",
        reference_period="2026-Q3",
    )
    assert m.indicator == "inflation"
    assert m.value == 42.5
    assert m.unit == "%"
    assert m.status == "raw"
    assert m.artifact_id is None  # provenance not set until extracted


def test_entity_importance_matches_database_contract(temp_db):
    """Entity importance round-trips as the numeric score stored by SQLite."""
    entity_id = temp_db.insert_entity(
        name="Iran",
        ent_type="country",
        importance=0.6,
    )
    entity = temp_db.get_entity_by_name("iran")
    assert entity is not None
    assert entity.id == entity_id
    assert entity.importance == 0.6


def test_normalize_text():
    """Text normalization collapses whitespace and lowercases."""
    assert _normalize_text("  Hello   World  ") == "hello world"
    assert _normalize_text("MULTI\nLINE\tTEXT") == "multi line text"


def test_content_hash_stability():
    """Same text produces same hash; different text produces different hash."""
    h1 = _content_hash("Hello world")
    h2 = _content_hash("Hello world")
    h3 = _content_hash("Hello World")
    h4 = _content_hash("Different")
    assert h1 == h2
    assert h1 == h3  # case-insensitive
    assert h1 != h4


# ── Database Tests ─────────────────────────────────────────────────────

def test_database_creates_all_tables(temp_db):
    """All 12 expected tables exist after init."""
    tables = temp_db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    table_names = {t[0] for t in tables}
    expected = {
        'sources', 'source_artifacts', 'entities', 'events',
        'event_evidence', 'event_entities', 'measurements',
        'claims', 'analyses', 'forecasts', 'evaluations',
        'sqlite_sequence'
    }
    assert expected.issubset(table_names)


def test_source_crud(temp_db):
    """Insert and retrieve source by name and URL."""
    source = Source(name="Reuters", url="https://reuters.com", type=SourceType.NEWS_AGENCY)
    sid = temp_db.insert_source(source)
    assert sid is not None

    by_name = temp_db.get_source_by_name("Reuters")
    assert by_name is not None
    assert by_name.id == sid
    assert by_name.reliability_profile == {"tier": 3}  # default, deserialized from JSON

    by_url = temp_db.get_source_by_url("https://reuters.com")
    assert by_url is not None
    assert by_url.id == sid


def test_artifact_insert_and_dedup(temp_db, sample_source):
    """Artifacts insert correctly; duplicate hash is rejected."""
    artifact = SourceArtifact(
        source_id=sample_source.id,
        original_title="Test Article",
        original_url="https://example.com/1",
        original_content="This is the article content.",
        content_hash="abc123",
        language="en",
    )
    aid1 = temp_db.insert_artifact(artifact)
    assert aid1 is not None

    # Duplicate hash returns None
    artifact2 = SourceArtifact(
        source_id=sample_source.id,
        original_title="Test Article 2",
        original_url="https://example.com/2",
        original_content="This is the article content.",  # same content
        content_hash="abc123",
        language="en",
    )
    aid2 = temp_db.insert_artifact(artifact2)
    assert aid2 is None

    # Verify only one row exists
    count = temp_db.conn.execute(
        "SELECT COUNT(*) FROM source_artifacts WHERE content_hash = 'abc123'"
    ).fetchone()[0]
    assert count == 1


def test_get_pending_artifacts(temp_db, sample_source):
    """get_pending_artifacts returns only pending-status artifacts."""
    # Insert 2 pending, 1 accepted, 1 extracted
    for i, status in enumerate([ProcessingStatus.PENDING, ProcessingStatus.PENDING,
                                 ProcessingStatus.ACCEPTED, ProcessingStatus.EXTRACTED]):
        a = SourceArtifact(
            source_id=sample_source.id,
            original_content=f"content {i}",
            content_hash=f"hash{i}",
        )
        a.processing_status = status
        temp_db.insert_artifact(a)

    pending = temp_db.get_pending_artifacts(limit=10)
    assert len(pending) == 2
    assert all(a[5] == ProcessingStatus.PENDING.value for a in pending)


# ── Gate 1 Tests ───────────────────────────────────────────────────────

def test_collect_from_source_inserts_artifacts(temp_db, sample_source):
    """Gate 1 can insert artifacts from a source (requires network)."""
    # This test hits the network - mark as integration
    pytest.skip("Network-dependent; run manually for integration test")


# ── Gate 2 Tests ───────────────────────────────────────────────────────

def test_run_gate2_no_key_leaves_artifacts_pending(temp_db, sample_source):
    """Missing extraction credentials must not imply successful extraction."""
    # Insert some pending artifacts
    for i in range(3):
        a = SourceArtifact(
            source_id=sample_source.id,
            original_content=f"article content {i}",
            content_hash=f"gate2hash{i}",
        )
        temp_db.insert_artifact(a)

    # Ensure no API key
    old_key = os.environ.pop("ROUTER_API_KEY", None)

    try:
        result = run_gate2(temp_db)
        assert result.artifacts_processed == 0
        assert result.skipped_no_key == 3
        assert result.claims_extracted == 0

        # All remain pending and can be retried once credentials are configured.
        pending = temp_db.get_pending_artifacts(limit=10)
        assert len(pending) == 3

        accepted_count = temp_db.conn.execute(
            "SELECT COUNT(*) FROM source_artifacts WHERE processing_status = 'accepted'"
        ).fetchone()[0]
        assert accepted_count == 0
    finally:
        if old_key:
            os.environ["ROUTER_API_KEY"] = old_key


def test_run_gate2_empty_queue(temp_db):
    """Gate 2 handles empty pending queue gracefully."""
    old_key = os.environ.pop("ROUTER_API_KEY", None)
    try:
        result = run_gate2(temp_db)
        assert result.artifacts_processed == 0
        assert result.claims_extracted == 0
    finally:
        if old_key:
            os.environ["ROUTER_API_KEY"] = old_key


def test_gate3_links_retained_measurement_subject(temp_db, sample_source):
    """Gate 3 resolves a stored source mention through a recorded alias."""
    artifact_id = temp_db.insert_artifact(SourceArtifact(
        source_id=sample_source.id,
        original_content="A measurement.",
        content_hash="measurement-subject-artifact",
    ))
    entity_id = temp_db.insert_entity(
        name="United States",
        ent_type="country",
        aliases=["US"],
    )
    measurement_id = temp_db.insert_measurement(
        artifact_id=artifact_id,
        indicator="GDP",
        subject_entity_id=None,
        subject_entity_name="US",
        value=1.0,
        unit=None,
        reference_period=None,
    )

    result = run_gate3(temp_db)
    linked = temp_db.conn.execute(
        "SELECT subject_entity_id FROM measurements WHERE id = ?",
        (measurement_id,),
    ).fetchone()[0]

    assert result.measurements_linked == 1
    assert result.entities_created == 0
    assert linked == entity_id


# ── Load Sources Tests ─────────────────────────────────────────────────

def test_load_sources_creates_db_entries(temp_db, tmp_path):
    """load_sources reads YAML and inserts sources."""
    yaml_path = tmp_path / "sources.yaml"
    yaml_path.write_text("""
sources:
  - name: "Test Feed"
    url: "https://example.com/rss"
    type: "news_agency"
    language: "en"
    country: "US"
""")

    sources = load_sources(yaml_path, temp_db)
    assert len(sources) == 1
    assert sources[0].name == "Test Feed"
    assert sources[0].id is not None

    # Second call should return existing
    sources2 = load_sources(yaml_path, temp_db)
    assert len(sources2) == 1
    assert sources2[0].id == sources[0].id


# ── Run Tests ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
