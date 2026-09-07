from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .models import (
    Source,
    SourceArtifact,
    Claim,
    Measurement,
    Entity,
    Event,
    AnalysisRecord,
    Forecast,
    SourceType,
    ProcessingStatus,
    ClaimOrigin,
    ClaimStatus,
    MeasurementStatus,
)


# ============================================================================
# Helpers
# ============================================================================

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _content_hash(text: str) -> str:
    """Return SHA-256 hash of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_dumps(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Optional[str], default: Any = None) -> Any:
    if value is None or value == "":
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


# ============================================================================
# Database schema
# ============================================================================

_SCHEMA = """
PRAGMA foreign_keys = ON;

-- =========================================================================
-- 3.1.1 SOURCE
-- =========================================================================

CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    type                TEXT NOT NULL DEFAULT 'other',
    language            TEXT NOT NULL DEFAULT 'en',
    country             TEXT,
    url                 TEXT UNIQUE,

    reliability_profile TEXT NOT NULL DEFAULT '{}',
    political_affiliation TEXT,

    collection_method   TEXT NOT NULL DEFAULT 'rss',
    update_frequency    TEXT,

    status              TEXT NOT NULL DEFAULT 'active',

    created_at          TEXT NOT NULL
);


-- =========================================================================
-- 3.1.2 SOURCE ARTIFACT
-- =========================================================================

CREATE TABLE IF NOT EXISTS source_artifacts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    source_id           INTEGER NOT NULL,

    publication_time    TEXT,
    collection_time     TEXT NOT NULL,

    original_title      TEXT,
    original_content    TEXT NOT NULL,
    original_url        TEXT,

    language            TEXT,
    media_type          TEXT NOT NULL DEFAULT 'text',

    content_hash        TEXT NOT NULL UNIQUE,

    processing_status   TEXT NOT NULL DEFAULT 'pending',

    FOREIGN KEY (source_id)
        REFERENCES sources(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_source
    ON source_artifacts(source_id);

CREATE INDEX IF NOT EXISTS idx_artifacts_status
    ON source_artifacts(processing_status);

CREATE INDEX IF NOT EXISTS idx_artifacts_hash
    ON source_artifacts(content_hash);


-- =========================================================================
-- 3.1.4 ENTITY
-- =========================================================================

CREATE TABLE IF NOT EXISTS entities (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    name            TEXT NOT NULL,
    type            TEXT NOT NULL,

    aliases         TEXT,
    description     TEXT,
    relationships   TEXT,

    importance      REAL DEFAULT 0.0,
    confidence      REAL DEFAULT 0.0,

    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_name
    ON entities(name);

CREATE INDEX IF NOT EXISTS idx_entities_type
    ON entities(type);


-- =========================================================================
-- 3.1.3 EVENT
-- =========================================================================

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    title           TEXT NOT NULL,
    summary         TEXT,

    start_time      TEXT,
    end_time        TEXT,
    location        TEXT,

    importance      TEXT,
    confidence      REAL DEFAULT 0.0,

    category        TEXT,
    status          TEXT NOT NULL DEFAULT 'ongoing',

    created_at      TEXT NOT NULL
);


-- =========================================================================
-- EVENT ↔ SOURCE ARTIFACT
--
-- An Event may have multiple supporting artifacts, and one artifact may
-- potentially contribute evidence to more than one event.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS event_evidence (
    event_id        INTEGER NOT NULL,
    artifact_id     INTEGER NOT NULL,

    relevance_score REAL DEFAULT 1.0,

    PRIMARY KEY (event_id, artifact_id),

    FOREIGN KEY (event_id)
        REFERENCES events(id)
        ON DELETE CASCADE,

    FOREIGN KEY (artifact_id)
        REFERENCES source_artifacts(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_evidence_artifact
    ON event_evidence(artifact_id);


-- =========================================================================
-- ARTIFACT ↔ ENTITY
--
-- This is intentionally artifact-specific.
-- It prevents the incorrect global "all entities belong to all events"
-- behavior encountered during Phase 1.1.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS artifact_entities (
    artifact_id     INTEGER NOT NULL,
    entity_id       INTEGER NOT NULL,

    confidence      REAL DEFAULT 0.8,

    PRIMARY KEY (artifact_id, entity_id),

    FOREIGN KEY (artifact_id)
        REFERENCES source_artifacts(id)
        ON DELETE CASCADE,

    FOREIGN KEY (entity_id)
        REFERENCES entities(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifact_entities_entity
    ON artifact_entities(entity_id);


-- =========================================================================
-- EVENT ↔ ENTITY
--
-- This relation represents entities actually involved in an Event.
-- It is NOT the same as artifact_entities.
-- ==========================================================================

CREATE TABLE IF NOT EXISTS event_entities (
    event_id        INTEGER NOT NULL,
    entity_id       INTEGER NOT NULL,

    confidence      REAL DEFAULT 0.8,

    PRIMARY KEY (event_id, entity_id),

    FOREIGN KEY (event_id)
        REFERENCES events(id)
        ON DELETE CASCADE,

    FOREIGN KEY (entity_id)
        REFERENCES entities(id)
        ON DELETE CASCADE
);


-- =========================================================================
-- 3.1.5 CLAIM
-- ==========================================================================

CREATE TABLE IF NOT EXISTS claims (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    artifact_id         INTEGER,
    proposition         TEXT NOT NULL,

    claim_origin        TEXT NOT NULL DEFAULT 'source',
    claim_type          TEXT,

    author_source       TEXT,
    time                TEXT,
    temporal_scope      TEXT,

    supporting_info     TEXT,
    contradicting_info  TEXT,

    status              TEXT NOT NULL DEFAULT 'candidate',

    processing_history  TEXT,

    created_at          TEXT NOT NULL,

    FOREIGN KEY (artifact_id)
        REFERENCES source_artifacts(id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_claims_artifact
    ON claims(artifact_id);

CREATE INDEX IF NOT EXISTS idx_claims_status
    ON claims(status);


-- =========================================================================
-- 3.1.7 MEASUREMENT
-- ==========================================================================

CREATE TABLE IF NOT EXISTS measurements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    indicator           TEXT NOT NULL,
    subject_entity_id   INTEGER,
    subject_entity_name TEXT,

    value               REAL NOT NULL,
    unit                TEXT,

    reference_period    TEXT,
    measurement_time    TEXT,

    -- Provenance. A measurement may originate from a designated
    -- measurement-grade source with NO source artifact, so artifact_id
    -- is nullable (ontology v1.8: Measurements are an authoritative
    -- quantitative-data layer, distinct from news-derived Claims).
    artifact_id         INTEGER,

    -- Designated measurement-grade source and acquisition pathway
    -- (e.g. central bank, official statistics agency, approved API).
    measurement_source  TEXT,
    acquisition_method  TEXT,

    collection_time     TEXT NOT NULL,

    -- Processing status (raw / etc.).
    status              TEXT NOT NULL DEFAULT 'raw',
    -- Quality / provenance notes: distinguish missing context from
    -- uncertainty about the value itself (ontology v1.8).
    quality             TEXT,

    FOREIGN KEY (subject_entity_id)
        REFERENCES entities(id)
        ON DELETE SET NULL,

    FOREIGN KEY (artifact_id)
        REFERENCES source_artifacts(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_measurements_artifact
    ON measurements(artifact_id);

CREATE INDEX IF NOT EXISTS idx_measurements_indicator
    ON measurements(indicator);

CREATE INDEX IF NOT EXISTS idx_measurements_subject
    ON measurements(subject_entity_id);


-- =========================================================================
-- 3.1.9 PROBLEM IDENTIFICATION FRAMEWORK (Discovery Layer, ontology v1.9)
-- =========================================================================
-- Lightweight structured signals that hand off to the Investigation Layer.
-- Reuses existing Claim / Measurement / Entity / SourceArtifact as evidence.
-- No scientific-paper extraction here (that belongs to the Investigation
-- Layer). A Problem REQUIRES >=1 evidence reference.

CREATE TABLE IF NOT EXISTS topics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    parent_id   INTEGER,
    FOREIGN KEY (parent_id) REFERENCES topics(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_topics_name ON topics(name);

CREATE TABLE IF NOT EXISTS problems (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    statement       TEXT NOT NULL,
    polarity        TEXT NOT NULL DEFAULT 'problem',   -- problem | opportunity
    topic_id        INTEGER,
    status          TEXT NOT NULL DEFAULT 'candidate',  -- candidate (pending investigation)
    artifact_id     INTEGER,                            -- originating artifact (provenance)
    created_at      TEXT NOT NULL,
    iran_relevant   TEXT,                              -- NULL=pending | yes | no | partial
    iran_note       TEXT,                              -- short justification / plausibility note
    FOREIGN KEY (topic_id)    REFERENCES topics(id)        ON DELETE SET NULL,
    FOREIGN KEY (artifact_id) REFERENCES source_artifacts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_problems_topic   ON problems(topic_id);
CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(status);

CREATE TABLE IF NOT EXISTS investigation_questions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  INTEGER NOT NULL,
    question    TEXT NOT NULL,
    rank        INTEGER DEFAULT 0,
    -- Step 11: Question-centered evidence lifecycle.
    evidence_strategy  TEXT,   -- inferred demand: scholarly_study |
                               --   quantitative_official | current_event | mixed
    status             TEXT DEFAULT 'pending',  -- pending | answered |
                               --   no_evidence | deferred | unmappable
    answer             TEXT,    -- minimal synthesis (nullable; AnalysisRecord deferred)
    investigated_at    TEXT,    -- ISO timestamp when status last set
    FOREIGN KEY (problem_id) REFERENCES problems(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_iq_problem ON investigation_questions(problem_id);
CREATE INDEX IF NOT EXISTS idx_iq_status ON investigation_questions(status);

-- Step 11: many-to-many link between an Investigation Question and the
-- evidence (Finding or Measurement) produced for it. Rerun-safe: the
-- composite PRIMARY KEY prevents duplicate Question->evidence links across
-- repeated Investigation runs. No generic Evidence object -- the link names
-- the concrete evidence_type so each substrate keeps its own identity.
CREATE TABLE IF NOT EXISTS question_evidence (
    question_id   INTEGER NOT NULL,
    evidence_type TEXT NOT NULL,   -- 'finding' | 'measurement'
    evidence_id   INTEGER NOT NULL,
    role          TEXT,            -- 'answers' | 'supports' (optional nuance)
    created_at    TEXT NOT NULL,
    PRIMARY KEY (question_id, evidence_type, evidence_id),
    FOREIGN KEY (question_id) REFERENCES investigation_questions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_qe_evidence ON question_evidence(evidence_type, evidence_id);

-- =========================================================================
-- 3.1.11 EVIDENCE RELATIONSHIP LAYER (Step 19)
-- ==========================================================================
-- Question-scoped semantic relationship between two pieces of evidence already
-- linked to the same Question. Makes inter-evidence relationships (agree /
-- conflict / address_different_aspect / not_comparable) EXPLICIT and QUERYABLE
-- without redesigning synthesis and without an LLM prose generator.
--
-- Identity INCLUDES the Question: the same evidence pair can legitimately bear a
-- different relationship under a different Question, and rerun-safety requires
-- per-Question uniqueness. (a,b) are normalized so A->B and B->A collapse to
-- one row (order_key = (evidence_a_type, evidence_a_id) <= (evidence_b_type,
-- evidence_b_id)).
CREATE TABLE IF NOT EXISTS evidence_relation (
    question_id       INTEGER NOT NULL,
    evidence_a_type   TEXT NOT NULL,   -- 'finding' | 'measurement'
    evidence_a_id     INTEGER NOT NULL,
    evidence_b_type   TEXT NOT NULL,
    evidence_b_id     INTEGER NOT NULL,
    relation          TEXT NOT NULL,   -- agree|conflict|address_different_aspect|not_comparable
    justification     TEXT,
    model             TEXT,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (question_id, evidence_a_type, evidence_a_id,
                evidence_b_type, evidence_b_id),
    FOREIGN KEY (question_id) REFERENCES investigation_questions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_er_question ON evidence_relation(question_id);

-- Problem <-> Entity (affected entities; must resolve to existing Entity)
CREATE TABLE IF NOT EXISTS problem_entities (
    problem_id INTEGER NOT NULL,
    entity_id  INTEGER NOT NULL,
    PRIMARY KEY (problem_id, entity_id),
    FOREIGN KEY (problem_id) REFERENCES problems(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id)  ON DELETE CASCADE
);

-- Problem <-> Evidence (reuses existing Claim / Measurement / Artifact IDs)
CREATE TABLE IF NOT EXISTS problem_evidence (
    problem_id     INTEGER NOT NULL,
    evidence_type  TEXT NOT NULL,   -- 'claim' | 'measurement' | 'artifact'
    evidence_id    INTEGER NOT NULL,
    PRIMARY KEY (problem_id, evidence_type, evidence_id),
    FOREIGN KEY (problem_id) REFERENCES problems(id) ON DELETE CASCADE
);


-- =========================================================================
-- 3.1.6 ANALYSIS RECORD
-- ==========================================================================
-- =========================================================================
-- 3.1.10 INVESTIGATION LAYER (scientific literature; ontology v1.10)
-- =========================================================================
-- Separate pipeline from the news Discovery Layer. Retrieves from OpenAlex /
-- Crossref (free, keyless) and extracts structured Studies / Findings /
-- Interventions with the local LLM. Interventions are FIRST-CLASS objects
-- with persistent identity so evidence ACCUMULATES across studies.

CREATE TABLE IF NOT EXISTS studies (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_work_id TEXT,
    title           TEXT NOT NULL,
    year            INTEGER,
    study_type      TEXT,
    authors         TEXT,            -- JSON list
    institutions    TEXT,            -- JSON list
    countries       TEXT,            -- JSON list
    doi             TEXT,
    landing_url     TEXT,
    pdf_url         TEXT,
    abstract        TEXT,
    concepts        TEXT,            -- JSON list (-> shared Topic)
    cited_by_count  INTEGER DEFAULT 0,
    evidence_quality TEXT,           -- LLM-graded: study type / tier / limitations
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_studies_work ON studies(source_work_id);

CREATE TABLE IF NOT EXISTS findings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id                INTEGER NOT NULL,
    statement               TEXT NOT NULL,
    population              TEXT,
    context                 TEXT,
    intervention_name       TEXT,    -- raw name; linked to interventions via study_interventions / finding_interventions
    outcome                 TEXT,
    effect_size            TEXT,
    causal_strength        TEXT,    -- graded: correlational | quasi-experimental | experimental/RCT
    geographic_applicability TEXT,
    limitations             TEXT,
    created_at              TEXT NOT NULL,
    FOREIGN KEY (study_id) REFERENCES studies(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_study ON findings(study_id);

CREATE TABLE IF NOT EXISTS interventions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    description         TEXT,
    type                TEXT,        -- technology | policy | practice
    target_problem_domain TEXT,
    status              TEXT DEFAULT 'identified',
    source_provenance   TEXT,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_interventions_name ON interventions(name);

-- Study/Finding evaluates/supports -> Intervention
CREATE TABLE IF NOT EXISTS finding_interventions (
    finding_id      INTEGER NOT NULL,
    intervention_id INTEGER NOT NULL,
    relation        TEXT NOT NULL DEFAULT 'evaluates',  -- supports | evaluates | reports_failure
    PRIMARY KEY (finding_id, intervention_id),
    FOREIGN KEY (finding_id) REFERENCES findings(id) ON DELETE CASCADE,
    FOREIGN KEY (intervention_id) REFERENCES interventions(id) ON DELETE CASCADE
);

-- Problem addressed_by -> Intervention
CREATE TABLE IF NOT EXISTS problem_interventions (
    problem_id      INTEGER NOT NULL,
    intervention_id INTEGER NOT NULL,
    relevance_to_iran   TEXT,        -- LLM-assessed
    adoption_barriers   TEXT,        -- LLM-assessed
    PRIMARY KEY (problem_id, intervention_id),
    FOREIGN KEY (problem_id) REFERENCES problems(id) ON DELETE CASCADE,
    FOREIGN KEY (intervention_id) REFERENCES interventions(id) ON DELETE CASCADE
);

-- Intervention applicable_to / implemented_by -> Entity (context)
CREATE TABLE IF NOT EXISTS intervention_entities (
    intervention_id INTEGER NOT NULL,
    entity_id        INTEGER NOT NULL,
    relation         TEXT NOT NULL DEFAULT 'applicable_to',  -- applicable_to | implemented_by | produces_targets
    PRIMARY KEY (intervention_id, entity_id, relation),
    FOREIGN KEY (intervention_id) REFERENCES interventions(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
);



CREATE TABLE IF NOT EXISTS analyses (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    question                TEXT NOT NULL,
    author                  TEXT,

    date                    TEXT NOT NULL,

    evidence_artifact_ids   TEXT,
    assumptions             TEXT,

    reasoning_chain         TEXT,

    claim_ids               TEXT,
    alternative_hypotheses  TEXT,

    confidence              REAL DEFAULT 0.0,

    forecast_ids            TEXT,

    created_at              TEXT NOT NULL
);


-- =========================================================================
-- 3.1.7 FORECAST
-- ==========================================================================

CREATE TABLE IF NOT EXISTS forecasts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,

    prediction              TEXT NOT NULL,
    probability             REAL NOT NULL DEFAULT 0.5,

    target_horizon          TEXT,
    conditions              TEXT,
    resolution_criteria     TEXT,

    date_issued             TEXT NOT NULL,

    author                  TEXT,

    supporting_analysis_id  INTEGER,

    outcome                 TEXT,

    evaluation_status       TEXT NOT NULL DEFAULT 'unresolved',
    evaluation_score        REAL,

    FOREIGN KEY (supporting_analysis_id)
        REFERENCES analyses(id)
        ON DELETE SET NULL
);


-- =========================================================================
-- FORECAST EVALUATION
-- ==========================================================================

CREATE TABLE IF NOT EXISTS evaluations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,

    forecast_id     INTEGER NOT NULL,

    outcome         TEXT,
    result          TEXT,
    score           REAL,

    evaluated_at    TEXT NOT NULL,

    FOREIGN KEY (forecast_id)
        REFERENCES forecasts(id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evaluations_forecast
    ON evaluations(forecast_id);
"""


# ============================================================================
# Database
# ============================================================================

class Database:

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            base = Path(__file__).parent.parent / "nexus_think_tank.db"
            self.db_path = str(base)
        else:
            self.db_path = db_path

        self.conn: sqlite3.Connection | None = None

    # ----------------------------------------------------------------------
    # Lifecycle
    # ----------------------------------------------------------------------

    def init(self) -> None:
        """Open the database and create the current schema."""

        self.conn = sqlite3.connect(self.db_path, isolation_level=None)

        self.conn.row_factory = sqlite3.Row

        # Foreign-key enforcement is connection-local in SQLite.
        self.conn.execute("PRAGMA foreign_keys = ON")

        self.conn.executescript(_SCHEMA)
        self._migrate_measurements_subject_name()
        self._migrate_measurements_v18()
        self._migrate_investigation_questions_v11()
        self._migrate_evidence_relation_v19()
        self._migrate_problems_iran()
        self.conn.commit()

    def _migrate_investigation_questions_v11(self) -> None:
        """Step 11 — Question-centered evidence lifecycle.

        Rerun-safe: add lifecycle columns to investigation_questions and create
        the question_evidence link table if they do not already exist. Existing
        Questions keep status='pending' (the DEFAULT) until an Investigation run
        sets it.
        """
        conn = self._require_connection()
        cols = {
            r["name"]
            for r in conn.execute("PRAGMA table_info(investigation_questions)")
        }
        for col, ddl in (
            ("evidence_strategy", "TEXT"),
            ("status", "TEXT DEFAULT 'pending'"),
            ("answer", "TEXT"),
            ("investigated_at", "TEXT"),
        ):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE investigation_questions ADD COLUMN {col} {ddl}"
                )
        # question_evidence link table (idempotent).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS question_evidence (
                question_id   INTEGER NOT NULL,
                evidence_type TEXT NOT NULL,
                evidence_id   INTEGER NOT NULL,
                role          TEXT,
                created_at    TEXT NOT NULL,
                PRIMARY KEY (question_id, evidence_type, evidence_id),
                FOREIGN KEY (question_id)
                    REFERENCES investigation_questions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_qe_evidence "
            "ON question_evidence(evidence_type, evidence_id)"
        )

    def _migrate_evidence_relation_v19(self) -> None:
        """Step 19 — Question-scoped evidence relationship table (idempotent)."""
        conn = self._require_connection()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_relation (
                question_id       INTEGER NOT NULL,
                evidence_a_type   TEXT NOT NULL,
                evidence_a_id     INTEGER NOT NULL,
                evidence_b_type   TEXT NOT NULL,
                evidence_b_id     INTEGER NOT NULL,
                relation          TEXT NOT NULL,
                justification     TEXT,
                model             TEXT,
                created_at        TEXT NOT NULL,
                PRIMARY KEY (question_id, evidence_a_type, evidence_a_id,
                            evidence_b_type, evidence_b_id),
                FOREIGN KEY (question_id)
                    REFERENCES investigation_questions(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_er_question "
            "ON evidence_relation(question_id)"
        )

    def _migrate_problems_iran(self) -> None:
        """Step 48 — Iran-compatibility gate columns (idempotent).

        Adds iran_relevant (NULL=pending | yes | no | partial) and iran_note to
        the problems table so the Discovery->Iran-scope gate can persist its
        verdict. Existing problems keep iran_relevant=NULL (pending assessment).
        """
        conn = self._require_connection()
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(problems)")}
        for col, ddl in (
            ("iran_relevant", "TEXT"),
            ("iran_note", "TEXT"),
            ("scenario", "TEXT"),
        ):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE problems ADD COLUMN {col} {ddl}"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_problems_iran "
            "ON problems(iran_relevant)"
        )

    def link_evidence_relation(
        self,
        question_id: int,
        evidence_a_type: str,
        evidence_a_id: int,
        evidence_b_type: str,
        evidence_b_id: int,
        relation: str,
        justification: str | None = None,
        model: str | None = None,
    ) -> None:
        """Persist a Question-scoped relationship between two evidence items.

        (a,b) are normalized so A->B and B->A collapse to ONE row (reruns never
        duplicate the reverse edge). Identity includes question_id, so the same
        evidence pair can bear different relations under different Questions.
        INSERT OR IGNORE makes the whole operation idempotent.
        """
        # Normalize order: (type,id) lexicographically.
        a = (evidence_a_type, evidence_a_id)
        b = (evidence_b_type, evidence_b_id)
        if a > b:
            a, b = b, a
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO evidence_relation "
            "(question_id, evidence_a_type, evidence_a_id, evidence_b_type, "
            "evidence_b_id, relation, justification, model, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (question_id, a[0], a[1], b[0], b[1], relation,
             justification, model, _iso(_utcnow())),
        )

    def get_evidence_relations(self, question_id: int | None = None) -> list:
        """Return evidence_relation rows (optionally for one Question)."""
        conn = self._require_connection()
        if question_id is None:
            rows = conn.execute(
                "SELECT * FROM evidence_relation ORDER BY question_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM evidence_relation WHERE question_id=? "
                "ORDER BY evidence_a_type, evidence_a_id, evidence_b_type",
                (question_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _migrate_measurements_subject_name(self) -> None:
        """Add Gate 3's raw-subject field to databases created before it."""
        conn = self._require_connection()
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(measurements)")
        }
        if "subject_entity_name" not in columns:
            conn.execute(
                "ALTER TABLE measurements ADD COLUMN subject_entity_name TEXT"
            )

    def _migrate_measurements_v18(self) -> None:
        """
        Ontology v1.8 Measurement boundary.

        Rebuild the measurements table so that:
          - artifact_id becomes NULL-able (a Measurement may come from a
            designated measurement-grade source with no news artifact);
          - measurement_source, acquisition_method, quality columns exist.

        Existing rows were created by Gate 2 from news prose (pre-v1.8), so
        they are NOT measurement-grade. They are retained but flagged in
        `quality` as legacy news-derived, and their artifact_id is kept.
        """
        conn = self._require_connection()
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(measurements)")
        }
        if "measurement_source" in columns and "quality" in columns:
            # Already migrated.
            return

        # Capture existing rows.
        existing = list(conn.execute("SELECT * FROM measurements").fetchall())

        conn.execute("ALTER TABLE measurements RENAME TO measurements_old")

        # Recreate with the v1.8 shape (mirrors _SCHEMA).
        conn.execute(
            """
            CREATE TABLE measurements (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                indicator           TEXT NOT NULL,
                subject_entity_id   INTEGER,
                subject_entity_name TEXT,
                value               REAL NOT NULL,
                unit                TEXT,
                reference_period    TEXT,
                measurement_time    TEXT,
                artifact_id         INTEGER,
                measurement_source  TEXT,
                acquisition_method  TEXT,
                collection_time     TEXT NOT NULL,
                status              TEXT NOT NULL DEFAULT 'raw',
                quality             TEXT,
                FOREIGN KEY (subject_entity_id)
                    REFERENCES entities(id) ON DELETE SET NULL,
                FOREIGN KEY (artifact_id)
                    REFERENCES source_artifacts(id) ON DELETE CASCADE
            )
            """
        )

        for row in existing:
            conn.execute(
                """
                INSERT INTO measurements (
                    id, indicator, subject_entity_id, subject_entity_name,
                    value, unit, reference_period, measurement_time,
                    artifact_id, measurement_source, acquisition_method,
                    collection_time, status, quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["indicator"],
                    row["subject_entity_id"],
                    row["subject_entity_name"],
                    row["value"],
                    row["unit"],
                    row["reference_period"],
                    row["measurement_time"],
                    row["artifact_id"],
                    None,
                    None,
                    row["collection_time"],
                    row["status"],
                    "legacy: news-derived, non-grade per ontology v1.8",
                ),
            )

        conn.execute("DROP TABLE measurements_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_measurements_artifact "
            "ON measurements(artifact_id)"
        )

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _require_connection(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError(
                "Database is not initialized. Call Database.init() first."
            )

        return self.conn

    # =========================================================================
    # SOURCE
    # =========================================================================

    def insert_source(self, source: Source) -> int:
        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO sources (
                name,
                type,
                language,
                country,
                url,
                reliability_profile,
                political_affiliation,
                collection_method,
                update_frequency,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source.name,
                source.type.value
                if hasattr(source.type, "value")
                else source.type,
                source.language,
                source.country,
                source.url,
                _json_dumps(source.reliability_profile) or "{}",
                source.political_affiliation,
                source.collection_method,
                source.update_frequency,
                source.status,
                _iso(source.created_at) or _iso(_utcnow()),
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    def count_sources(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        )

    def get_source_by_name(self, name: str) -> Optional[Source]:
        conn = self._require_connection()

        row = conn.execute(
            """
            SELECT
                id,
                name,
                type,
                language,
                country,
                url,
                reliability_profile,
                political_affiliation,
                collection_method,
                update_frequency,
                status,
                created_at
            FROM sources
            WHERE name = ?
            """,
            (name,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_source(row)

    def get_source_by_url(self, url: str) -> Optional[Source]:
        conn = self._require_connection()

        row = conn.execute(
            """
            SELECT
                id,
                name,
                type,
                language,
                country,
                url,
                reliability_profile,
                political_affiliation,
                collection_method,
                update_frequency,
                status,
                created_at
            FROM sources
            WHERE url = ?
            """,
            (url,),
        ).fetchone()

        if row is None:
            return None

        return self._row_to_source(row)

    def _row_to_source(self, row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"],
            name=row["name"],
            type=SourceType(row["type"]),
            language=row["language"],
            country=row["country"],
            url=row["url"],
            reliability_profile=_json_loads(
                row["reliability_profile"],
                {},
            ),
            political_affiliation=row["political_affiliation"],
            collection_method=row["collection_method"],
            update_frequency=row["update_frequency"],
            status=row["status"],
            created_at=_parse_datetime(row["created_at"]),
        )

    # =========================================================================
    # SOURCE ARTIFACT
    # =========================================================================

    def insert_artifact(self, artifact: SourceArtifact) -> int:
        conn = self._require_connection()

        content = artifact.original_content or ""
        content_hash = artifact.content_hash or _content_hash(content)

        # Dedup contract: if an artifact with this content_hash already exists,
        # do not insert a duplicate row — return None so callers (and Gate 1)
        # can treat it as "already collected". The UNIQUE index remains the
        # authoritative guard against a concurrent insert.
        existing = conn.execute(
            "SELECT id FROM source_artifacts WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        if existing is not None:
            return None

        cur = conn.execute(
            """
            INSERT INTO source_artifacts (
                source_id,
                publication_time,
                collection_time,
                original_title,
                original_content,
                original_url,
                language,
                media_type,
                content_hash,
                processing_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact.source_id,
                _iso(artifact.publication_time),
                _iso(artifact.collection_time) or _iso(_utcnow()),
                artifact.original_title,
                content,
                artifact.original_url,
                artifact.language,
                artifact.media_type,
                content_hash,
                artifact.processing_status.value
                if hasattr(artifact.processing_status, "value")
                else artifact.processing_status,
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    def count_artifacts(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM source_artifacts"
            ).fetchone()[0]
        )

    def get_pending_artifacts(self, limit: int = 100) -> List[Tuple]:
        """
        Return pending artifacts.

        Tuple layout:
            (
                id,
                source_id,
                original_title,
                original_url,
                original_content,
                processing_status
            )
        """

        if limit <= 0:
            return []

        conn = self._require_connection()

        cur = conn.execute(
            """
            SELECT
                id,
                source_id,
                original_title,
                original_url,
                original_content,
                processing_status
            FROM source_artifacts
            WHERE processing_status = 'pending'
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        )

        return [tuple(row) for row in cur.fetchall()]

    def get_artifact(self, artifact_id: int) -> Optional[Tuple]:
        conn = self._require_connection()

        row = conn.execute(
            """
            SELECT
                id,
                source_id,
                original_title,
                original_url,
                original_content,
                processing_status
            FROM source_artifacts
            WHERE id = ?
            """,
            (artifact_id,),
        ).fetchone()

        return tuple(row) if row is not None else None

    def update_artifact_status(
        self,
        artifact_id: int,
        status: str,
    ) -> None:
        conn = self._require_connection()

        conn.execute(
            """
            UPDATE source_artifacts
            SET processing_status = ?
            WHERE id = ?
            """,
            (status, artifact_id),
        )

        conn.commit()

    # =========================================================================
    # CLAIM
    # =========================================================================

    def insert_claim(
        self,
        artifact_id: int,
        proposition: str,
        claim_type: str,
        author_source: str | None = None,
        temporal_scope: str | None = None,
        claim_origin: str = "source",
        time: datetime | None = None,
        supporting_info: List[int] | None = None,
        contradicting_info: List[int] | None = None,
        status: str = "candidate",
        processing_history: List[dict] | None = None,
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO claims (
                artifact_id,
                proposition,
                claim_origin,
                claim_type,
                author_source,
                time,
                temporal_scope,
                supporting_info,
                contradicting_info,
                status,
                processing_history,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                proposition.strip(),
                claim_origin,
                claim_type,
                author_source,
                _iso(time),
                temporal_scope,
                _json_dumps(supporting_info or []),
                _json_dumps(contradicting_info or []),
                status,
                _json_dumps(processing_history or []),
                _iso(_utcnow()),
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    def count_claims(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0]
        )

    # =========================================================================
    # MEASUREMENT
    # =========================================================================

    def insert_measurement(
        self,
        artifact_id: int | None,
        indicator: str,
        subject_entity_id: int | None,
        subject_entity_name: str | None,
        value: float,
        unit: str | None,
        reference_period: str | None,
        measurement_time: datetime | None = None,
        collection_time: datetime | None = None,
        status: str = "raw",
        measurement_source: str | None = None,
        acquisition_method: str | None = None,
        quality: str | None = None,
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO measurements (
                indicator,
                subject_entity_id,
                subject_entity_name,
                value,
                unit,
                reference_period,
                measurement_time,
                artifact_id,
                measurement_source,
                acquisition_method,
                quality,
                collection_time,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                indicator.strip(),
                subject_entity_id,
                subject_entity_name.strip() if subject_entity_name else None,
                value,
                unit,
                reference_period,
                _iso(measurement_time),
                artifact_id,
                measurement_source,
                acquisition_method,
                quality,
                _iso(collection_time) or _iso(_utcnow()),
                status,
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    # ── Step 11: Investigation Question lifecycle ──────────────────────

    def get_questions_for_problem(self, problem_id: int) -> list:
        """Return Question rows (id, question, rank) for a Problem, in rank order."""
        conn = self._require_connection()
        return [
            {"id": r["id"], "question": r["question"], "rank": r["rank"]}
            for r in conn.execute(
                "SELECT id, question, rank FROM investigation_questions "
                "WHERE problem_id=? ORDER BY rank",
                (problem_id,),
            ).fetchall()
        ]

    def link_question_evidence(
        self,
        question_id: int,
        evidence_type: str,   # 'finding' | 'measurement'
        evidence_id: int,
        role: str | None = None,
    ) -> None:
        """Link a Question to one piece of evidence (many-to-many, rerun-safe).

        INSERT OR IGNORE on the composite PK means repeated Investigation runs
        never create duplicate Question->evidence links, but new evidence from a
        later run is still added. One piece of evidence may be linked to many
        Questions (a study can answer several Questions).
        """
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO question_evidence "
            "(question_id, evidence_type, evidence_id, role, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (question_id, evidence_type, evidence_id, role, _iso(_utcnow())),
        )

    def update_question_lifecycle(
        self,
        question_id: int,
        evidence_strategy: str | None,
        status: str,
        investigated_at: datetime | None = None,
    ) -> None:
        """Persist a Question's inferred evidence-demand and lifecycle status.

        status ∈ {pending, answered, no_evidence, deferred, unmappable}.
        A Question is only moved off 'pending' after all required substrates
        have been attempted (the pipeline enforces this); operational/source
        failures are recorded as 'deferred', never as 'no_evidence'.
        """
        conn = self._require_connection()
        conn.execute(
            "UPDATE investigation_questions SET evidence_strategy=?, status=?, "
            "investigated_at=? WHERE id=?",
            (evidence_strategy, status, _iso(investigated_at) or _iso(_utcnow()),
             question_id),
        )

    def set_question_answer(self, question_id: int, answer: str | None) -> None:
        """Store a minimal synthesized answer for a Question (nullable)."""
        conn = self._require_connection()
        conn.execute(
            "UPDATE investigation_questions SET answer=? WHERE id=?",
            (answer, question_id),
        )

    def get_question_evidence(self, question_id: int) -> list:
        """Return the evidence linked to a Question (type + id + role)."""
        conn = self._require_connection()
        return [
            {"evidence_type": r["evidence_type"], "evidence_id": r["evidence_id"],
             "role": r["role"]}
            for r in conn.execute(
                "SELECT evidence_type, evidence_id, role FROM question_evidence "
                "WHERE question_id=?",
                (question_id,),
            ).fetchall()
        ]

    def get_or_create_measurement(
        self,
        artifact_id: int | None,
        indicator: str,
        subject_entity_id: int | None,
        subject_entity_name: str | None,
        value: float,
        unit: str | None,
        reference_period: str | None,
        measurement_time: datetime | None = None,
        collection_time: datetime | None = None,
        status: str = "raw",
        measurement_source: str | None = None,
        acquisition_method: str | None = None,
        quality: str | None = None,
    ) -> int:
        """Like insert_measurement, but reuse an identical existing observation
        (same indicator + subject + reference_period + source) if present. Keeps
        measurement ids STABLE across repeated Investigation runs so
        Question->evidence links stay rerun-safe (Step 11)."""
        conn = self._require_connection()
        row = conn.execute(
            "SELECT id FROM measurements WHERE indicator=? AND "
            "COALESCE(subject_entity_name,'')=COALESCE(?,'') AND "
            "COALESCE(reference_period,'')=COALESCE(?,'') AND "
            "COALESCE(measurement_source,'')=COALESCE(?, '')",
            (indicator.strip(), subject_entity_name, reference_period,
             measurement_source),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        return self.insert_measurement(
            artifact_id=artifact_id, indicator=indicator,
            subject_entity_id=subject_entity_id,
            subject_entity_name=subject_entity_name, value=value, unit=unit,
            reference_period=reference_period, measurement_time=measurement_time,
            collection_time=collection_time, status=status,
            measurement_source=measurement_source,
            acquisition_method=acquisition_method, quality=quality,
        )

    def update_measurement_subject(
        self,
        measurement_id: int,
        entity_id: int,
    ) -> None:

        conn = self._require_connection()

        conn.execute(
            """
            UPDATE measurements
            SET subject_entity_id = ?
            WHERE id = ?
            """,
            (entity_id, measurement_id),
        )

        conn.commit()

    def get_unresolved_measurements(self) -> list[sqlite3.Row]:
        """Return measurements with a retained subject mention but no entity link."""
        conn = self._require_connection()
        return list(
            conn.execute(
                """
                SELECT id, subject_entity_name
                FROM measurements
                WHERE subject_entity_id IS NULL
                  AND subject_entity_name IS NOT NULL
                  AND TRIM(subject_entity_name) != ''
                """
            ).fetchall()
        )

    def count_measurements(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM measurements"
            ).fetchone()[0]
        )

    # =========================================================================
    # ENTITY
    # =========================================================================

    # =========================================================================
    # PROBLEM IDENTIFICATION FRAMEWORK (ontology v1.9)
    # =========================================================================

    def upsert_topic(self, name: str, parent_id: int | None = None) -> int:
        """Get-or-create a Topic by canonical name."""
        conn = self._require_connection()
        row = conn.execute(
            "SELECT id FROM topics WHERE name = ?", (name.strip(),)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = conn.execute(
            "INSERT INTO topics (name, parent_id) VALUES (?, ?)",
            (name.strip(), parent_id),
        )
        conn.commit()
        return int(cur.lastrowid)

    def set_problem_iran(self, problem_id: int, iran_relevant: str, iran_note: str | None) -> None:
        """Step 48 — persist the Iran-compatibility gate verdict for a problem."""
        conn = self._require_connection()
        conn.execute(
            "UPDATE problems SET iran_relevant=?, iran_note=? WHERE id=?",
            (iran_relevant, iran_note, problem_id),
        )

    def get_problem_iran(self, problem_id: int) -> tuple[str | None, str | None]:
        """Return (iran_relevant, iran_note) for a problem, or (None, None)."""
        conn = self._require_connection()
        r = conn.execute(
            "SELECT iran_relevant, iran_note FROM problems WHERE id=?",
            (problem_id,),
        ).fetchone()
        if not r:
            return (None, None)
        return (r["iran_relevant"], r["iran_note"])

    def insert_problem(
        self,
        statement: str,
        polarity: str,
        topic_id: int | None,
        artifact_id: int | None,
        status: str = "candidate",
    ) -> int:
        conn = self._require_connection()
        cur = conn.execute(
            """
            INSERT INTO problems (statement, polarity, topic_id, status, artifact_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                statement.strip(),
                polarity,
                topic_id,
                status,
                artifact_id,
                _iso(_utcnow()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def insert_investigation_question(
        self, problem_id: int, question: str, rank: int = 0
    ) -> int:
        conn = self._require_connection()
        cur = conn.execute(
            "INSERT INTO investigation_questions (problem_id, question, rank) VALUES (?, ?, ?)",
            (problem_id, question.strip(), rank),
        )
        conn.commit()
        return int(cur.lastrowid)

    def link_problem_entity(self, problem_id: int, entity_id: int) -> None:
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO problem_entities (problem_id, entity_id) VALUES (?, ?)",
            (problem_id, entity_id),
        )
        conn.commit()

    def link_problem_evidence(
        self, problem_id: int, evidence_type: str, evidence_id: int
    ) -> None:
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO problem_evidence (problem_id, evidence_type, evidence_id) VALUES (?, ?, ?)",
            (problem_id, evidence_type, evidence_id),
        )
        conn.commit()

    def count_problems(self) -> int:
        conn = self._require_connection()
        return int(conn.execute("SELECT COUNT(*) FROM problems").fetchone()[0])

    # =========================================================================
    # INVESTIGATION LAYER (ontology v1.10)
    # =========================================================================

    def insert_study(
        self,
        title: str,
        source_work_id: str | None = None,
        year: int | None = None,
        study_type: str | None = None,
        authors: list | None = None,
        institutions: list | None = None,
        countries: list | None = None,
        doi: str | None = None,
        landing_url: str | None = None,
        pdf_url: str | None = None,
        abstract: str | None = None,
        concepts: list | None = None,
        cited_by_count: int = 0,
        evidence_quality: str | None = None,
    ) -> int:
        conn = self._require_connection()
        cur = conn.execute(
            """
            INSERT INTO studies (
                source_work_id, title, year, study_type, authors, institutions,
                countries, doi, landing_url, pdf_url, abstract, concepts,
                cited_by_count, evidence_quality, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_work_id,
                title.strip(),
                year,
                study_type,
                _json_dumps(authors or []),
                _json_dumps(institutions or []),
                _json_dumps(countries or []),
                doi,
                landing_url,
                pdf_url,
                abstract,
                _json_dumps(concepts or []),
                cited_by_count,
                evidence_quality,
                _iso(_utcnow()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def get_or_create_study(
        self,
        title: str,
        source_work_id: str | None = None,
        year: int | None = None,
        study_type: str | None = None,
        authors: list | None = None,
        institutions: list | None = None,
        countries: list | None = None,
        doi: str | None = None,
        landing_url: str | None = None,
        pdf_url: str | None = None,
        abstract: str | None = None,
        concepts: list | None = None,
        cited_by_count: int = 0,
        evidence_quality: str | None = None,
    ) -> int:
        """Like insert_study, but reuse an existing study with the same
        source_work_id (OpenAlex ID) if present. Keeps study + finding ids
        STABLE across repeated Investigation runs so Question->evidence links
        stay rerun-safe (Step 11)."""
        conn = self._require_connection()
        if source_work_id:
            row = conn.execute(
                "SELECT id FROM studies WHERE source_work_id=?",
                (source_work_id,),
            ).fetchone()
            if row is not None:
                return int(row["id"])
        return self.insert_study(
            title=title, source_work_id=source_work_id, year=year,
            study_type=study_type, authors=authors, institutions=institutions,
            countries=countries, doi=doi, landing_url=landing_url,
            pdf_url=pdf_url, abstract=abstract, concepts=concepts,
            cited_by_count=cited_by_count, evidence_quality=evidence_quality,
        )

    def insert_finding(
        self,
        study_id: int,
        statement: str,
        population: str | None = None,
        context: str | None = None,
        intervention_name: str | None = None,
        outcome: str | None = None,
        effect_size: str | None = None,
        causal_strength: str | None = None,
        geographic_applicability: str | None = None,
        limitations: str | None = None,
    ) -> int:
        conn = self._require_connection()
        cur = conn.execute(
            """
            INSERT INTO findings (
                study_id, statement, population, context, intervention_name,
                outcome, effect_size, causal_strength,
                geographic_applicability, limitations, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                study_id,
                statement.strip(),
                population,
                context,
                intervention_name,
                outcome,
                effect_size,
                causal_strength,
                geographic_applicability,
                limitations,
                _iso(_utcnow()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def get_or_create_finding(
        self,
        study_id: int,
        statement: str,
        population: str | None = None,
        context: str | None = None,
        intervention_name: str | None = None,
        outcome: str | None = None,
        effect_size: str | None = None,
        causal_strength: str | None = None,
        geographic_applicability: str | None = None,
        limitations: str | None = None,
    ) -> int:
        """Insert a finding, but reuse an identical existing one (same study_id
        + statement) if present. Keeps finding ids STABLE across repeated
        Investigation runs so Question->evidence links remain rerun-safe and
        evidence ACCUMULATES rather than duplicating (Step 6 / Step 11)."""
        conn = self._require_connection()
        row = conn.execute(
            "SELECT id FROM findings WHERE study_id=? AND statement=?",
            (study_id, statement.strip()),
        ).fetchone()
        if row is not None:
            return int(row["id"])
        return self.insert_finding(
            study_id=study_id, statement=statement, population=population,
            context=context, intervention_name=intervention_name,
            outcome=outcome, effect_size=effect_size,
            causal_strength=causal_strength,
            geographic_applicability=geographic_applicability,
            limitations=limitations,
        )

    def upsert_intervention(
        self,
        name: str,
        description: str | None = None,
        type_: str | None = None,
        target_problem_domain: str | None = None,
        status: str = "identified",
        source_provenance: str | None = None,
    ) -> int:
        """Get-or-create an Intervention by canonical name (casefold).

        Interventions have PERSISTENT IDENTITY: the same intervention across
        dozens of studies must accumulate evidence on ONE row. """
        conn = self._require_connection()
        folded = name.strip().casefold()
        row = conn.execute(
            "SELECT id FROM interventions WHERE name = ?", (name.strip(),)
        ).fetchone()
        if row is not None:
            return int(row["id"])
        cur = conn.execute(
            """
            INSERT INTO interventions (
                name, description, type, target_problem_domain, status,
                source_provenance, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                description,
                type_,
                target_problem_domain,
                status,
                source_provenance,
                _iso(_utcnow()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)

    def link_finding_intervention(
        self, finding_id: int, intervention_id: int, relation: str = "evaluates"
    ) -> None:
        conn = self._require_connection()
        # Epistemic rule (Step 2): only verdict/assessment relations are stored.
        # 'mention' / theoretical proposition => no row (caller must not call).
        if relation not in ("supports", "evaluates", "reports_failure"):
            return
        conn.execute(
            "INSERT OR IGNORE INTO finding_interventions (finding_id, intervention_id, relation) VALUES (?, ?, ?)",
            (finding_id, intervention_id, relation),
        )
        conn.commit()

    def link_problem_intervention(
        self,
        problem_id: int,
        intervention_id: int,
        relevance_to_iran: str | None = None,
        adoption_barriers: str | None = None,
    ) -> None:
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO problem_interventions (problem_id, intervention_id, relevance_to_iran, adoption_barriers) VALUES (?, ?, ?, ?)",
            (problem_id, intervention_id, relevance_to_iran, adoption_barriers),
        )
        conn.commit()

    def link_intervention_entity(
        self, intervention_id: int, entity_id: int, relation: str = "applicable_to"
    ) -> None:
        conn = self._require_connection()
        conn.execute(
            "INSERT OR IGNORE INTO intervention_entities (intervention_id, entity_id, relation) VALUES (?, ?, ?)",
            (intervention_id, entity_id, relation),
        )
        conn.commit()

    def count_studies(self) -> int:
        conn = self._require_connection()
        return int(conn.execute("SELECT COUNT(*) FROM studies").fetchone()[0])

    def count_interventions(self) -> int:
        conn = self._require_connection()
        return int(conn.execute("SELECT COUNT(*) FROM interventions").fetchone()[0])

    def insert_entity(
        self,
        name: str,
        ent_type: str,
        aliases: List[str] | None = None,
        description: str | None = None,
        relationships: Any = None,
        importance: float | None = None,
        confidence: float | None = None,
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO entities (
                name,
                type,
                aliases,
                description,
                relationships,
                importance,
                confidence,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name.strip(),
                ent_type.strip(),
                _json_dumps(aliases or []),
                description,
                _json_dumps(relationships)
                if not isinstance(relationships, str)
                else relationships,
                importance if importance is not None else 0.0,
                confidence if confidence is not None else 0.0,
                _iso(_utcnow()),
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    def count_entities(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        )

    def get_entity_by_name(self, name: str) -> Optional[Entity]:
        conn = self._require_connection()

        row = conn.execute(
            """
            SELECT
                id,
                name,
                type,
                aliases,
                description,
                relationships,
                importance,
                confidence
            FROM entities
            WHERE name = ? COLLATE NOCASE
            """,
            (name,),
        ).fetchone()

        if row is None:
            return None

        return Entity(
            id=row["id"],
            name=row["name"],
            entity_type=row["type"],
            aliases=_json_loads(row["aliases"], []),
            description=row["description"],
            relationships=_json_loads(row["relationships"], []),
            importance=row["importance"] or 0.0,
            confidence=row["confidence"] or 0.0,
        )

    def find_entity_by_alias(self, alias: str) -> Optional[Entity]:
        """Return the entity whose stored aliases contain ``alias`` (case-insensitive)."""
        alias_folded = alias.strip().casefold()
        if not alias_folded:
            return None

        conn = self._require_connection()
        rows = conn.execute("SELECT name, aliases FROM entities").fetchall()
        for row in rows:
            aliases = _json_loads(row["aliases"], [])
            if any(
                isinstance(value, str) and value.strip().casefold() == alias_folded
                for value in aliases
            ):
                return self.get_entity_by_name(row["name"])
        return None

    def get_entity_by_name_or_alias(self, name: str) -> Optional[Entity]:
        """Return an entity whose primary name OR any recorded alias equals
        ``name`` (case-insensitive). Used by Gate 2 so that an incoming name
        that already exists as an alias of another entity is reused instead of
        creating a duplicate. Exact-equivalence only (no fuzzy matching)."""
        name_folded = name.strip().casefold()
        if not name_folded:
            return None
        conn = self._require_connection()
        row = conn.execute(
            "SELECT name, aliases FROM entities"
        ).fetchall()
        for r in row:
            if r["name"].strip().casefold() == name_folded:
                return self.get_entity_by_name(r["name"])
            stored = _json_loads(r["aliases"], [])
            if any(
                isinstance(v, str) and v.strip().casefold() == name_folded
                for v in stored
            ):
                return self.get_entity_by_name(r["name"])
        return None

    def add_alias_to_entity(self, entity_id: int, alias: str) -> bool:
        """Add a normalized alias if it is not already present; return whether it changed."""
        alias_clean = alias.strip()
        if not alias_clean:
            return False

        conn = self._require_connection()
        row = conn.execute(
            "SELECT aliases FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"Entity {entity_id} does not exist.")

        aliases = _json_loads(row["aliases"], [])
        if any(
            isinstance(value, str) and value.strip().casefold() == alias_clean.casefold()
            for value in aliases
        ):
            return False

        aliases.append(alias_clean)
        conn.execute(
            "UPDATE entities SET aliases = ? WHERE id = ?",
            (_json_dumps(aliases), entity_id),
        )
        conn.commit()
        return True

    # =========================================================================
    # ARTIFACT ↔ ENTITY
    # =========================================================================

    def link_artifact_entity(
        self,
        artifact_id: int,
        entity_id: int,
        confidence: float = 0.8,
    ) -> None:

        conn = self._require_connection()

        conn.execute(
            """
            INSERT INTO artifact_entities (
                artifact_id,
                entity_id,
                confidence
            )
            VALUES (?, ?, ?)
            ON CONFLICT (artifact_id, entity_id)
            DO UPDATE SET confidence = excluded.confidence
            """,
            (
                artifact_id,
                entity_id,
                confidence,
            ),
        )

        conn.commit()

    def get_artifact_entities(
        self,
        artifact_id: int,
    ) -> List[Tuple]:

        conn = self._require_connection()

        rows = conn.execute(
            """
            SELECT entity_id, confidence
            FROM artifact_entities
            WHERE artifact_id = ?
            ORDER BY entity_id
            """,
            (artifact_id,),
        ).fetchall()

        return [tuple(row) for row in rows]

    # =========================================================================
    # EVENT
    # =========================================================================

    def insert_event(
        self,
        title: str,
        summary: str | None,
        source_artifact_id: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        location: str | None = None,
        importance: str | None = None,
        confidence: float = 0.0,
        category: str | None = None,
        status: str = "ongoing",
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO events (
                title,
                summary,
                start_time,
                end_time,
                location,
                importance,
                confidence,
                category,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title.strip(),
                summary.strip() if summary else None,
                _iso(start_time),
                _iso(end_time),
                location,
                importance,
                confidence,
                category,
                status,
                _iso(_utcnow()),
            ),
        )

        event_id = int(cur.lastrowid)

        # Preserve compatibility with existing Gate 3.5 code:
        # if an artifact was supplied, immediately establish evidence.
        if source_artifact_id is not None:
            conn.execute(
                """
                INSERT OR IGNORE INTO event_evidence (
                    event_id,
                    artifact_id,
                    relevance_score
                )
                VALUES (?, ?, ?)
                """,
                (
                    event_id,
                    source_artifact_id,
                    1.0,
                ),
            )

        conn.commit()
        return event_id

    def count_events(self) -> int:
        conn = self._require_connection()
        return int(
            conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        )

    # =========================================================================
    # EVENT ↔ ARTIFACT
    # =========================================================================

    def link_event_evidence(
        self,
        event_id: int,
        artifact_id: int,
        relevance_score: float = 1.0,
    ) -> None:

        conn = self._require_connection()

        conn.execute(
            """
            INSERT INTO event_evidence (
                event_id,
                artifact_id,
                relevance_score
            )
            VALUES (?, ?, ?)
            ON CONFLICT (event_id, artifact_id)
            DO UPDATE SET relevance_score = excluded.relevance_score
            """,
            (
                event_id,
                artifact_id,
                relevance_score,
            ),
        )

        conn.commit()

    # =========================================================================
    # EVENT ↔ ENTITY
    # =========================================================================

    def link_event_entity(
        self,
        event_id: int,
        entity_id: int,
        confidence: float = 0.8,
    ) -> None:

        conn = self._require_connection()

        conn.execute(
            """
            INSERT INTO event_entities (
                event_id,
                entity_id,
                confidence
            )
            VALUES (?, ?, ?)
            ON CONFLICT (event_id, entity_id)
            DO UPDATE SET confidence = excluded.confidence
            """,
            (
                event_id,
                entity_id,
                confidence,
            ),
        )

        conn.commit()

    def get_event_entities(
        self,
        event_id: int,
    ) -> List[Tuple]:

        conn = self._require_connection()

        rows = conn.execute(
            """
            SELECT entity_id, confidence
            FROM event_entities
            WHERE event_id = ?
            ORDER BY entity_id
            """,
            (event_id,),
        ).fetchall()

        return [tuple(row) for row in rows]

    # =========================================================================
    # ANALYSIS RECORD
    # =========================================================================

    def insert_analysis(
        self,
        question: str,
        author: str | None = None,
        date: datetime | None = None,
        evidence_artifact_ids: List[int] | None = None,
        assumptions: List[str] | None = None,
        reasoning_chain: str | None = None,
        claim_ids: List[int] | None = None,
        alternative_hypotheses: List[str] | None = None,
        confidence: float = 0.0,
        forecast_ids: List[int] | None = None,
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO analyses (
                question,
                author,
                date,
                evidence_artifact_ids,
                assumptions,
                reasoning_chain,
                claim_ids,
                alternative_hypotheses,
                confidence,
                forecast_ids,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                question,
                author,
                _iso(date) or _iso(_utcnow()),
                _json_dumps(evidence_artifact_ids or []),
                _json_dumps(assumptions or []),
                reasoning_chain,
                _json_dumps(claim_ids or []),
                _json_dumps(alternative_hypotheses or []),
                confidence,
                _json_dumps(forecast_ids or []),
                _iso(_utcnow()),
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    # =========================================================================
    # FORECAST
    # =========================================================================

    def insert_forecast(
        self,
        prediction: str,
        probability: float,
        target_horizon: str | None = None,
        conditions: str | None = None,
        resolution_criteria: str | None = None,
        date_issued: datetime | None = None,
        author: str | None = None,
        supporting_analysis_id: int | None = None,
    ) -> int:

        conn = self._require_connection()

        cur = conn.execute(
            """
            INSERT INTO forecasts (
                prediction,
                probability,
                target_horizon,
                conditions,
                resolution_criteria,
                date_issued,
                author,
                supporting_analysis_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                prediction,
                probability,
                target_horizon,
                conditions,
                resolution_criteria,
                _iso(date_issued) or _iso(_utcnow()),
                author,
                supporting_analysis_id,
            ),
        )

        conn.commit()
        return int(cur.lastrowid)

    # =========================================================================
    # FORECAST EVALUATION
    # =========================================================================

    def insert_evaluation(
        self,
        forecast_id: int,
        outcome: str | None,
        result: str | None,
        score: float | None,
    ) -> int:

        conn = self._require_connection()

        evaluated_at = _iso(_utcnow())

        cur = conn.execute(
            """
            INSERT INTO evaluations (
                forecast_id,
                outcome,
                result,
                score,
                evaluated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                forecast_id,
                outcome,
                result,
                score,
                evaluated_at,
            ),
        )

        # Keep Forecast's evaluation state synchronized.
        conn.execute(
            """
            UPDATE forecasts
            SET
                outcome = ?,
                evaluation_status = ?,
                evaluation_score = ?
            WHERE id = ?
            """,
            (
                outcome,
                result or "resolved",
                score,
                forecast_id,
            ),
        )

        conn.commit()
        return int(cur.lastrowid)
