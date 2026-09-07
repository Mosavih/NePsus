"""
Nexus-Think-Tank — Gate 1: Collection

Gate 1 is deterministic:

1. Fetch entries from a configured RSS feed.
2. Extract full-text content from each article URL using trafilatura.
3. Normalize the extracted text.
4. Compute a SHA-256 content hash.
5. Insert a SourceArtifact with processing_status='pending'.
6. Let the database's UNIQUE(content_hash) constraint handle deduplication.

No LLM is used in Gate 1.

The Database layer is the source of truth for persistence and deduplication.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import feedparser
import requests
import trafilatura

from .models import Source, SourceArtifact, ProcessingStatus
from .database import Database


# ── Configuration ──────────────────────────────────────────────────────

MAX_ENTRIES_PER_SOURCE = 10

REQUEST_DELAY_S = 1.0

HTTP_TIMEOUT_S = 15

USER_AGENT = "NexusThinkTank/0.1 (+https://github.com/nexus-think-tank)"

MIN_CONTENT_LENGTH = 200


# ── Helpers ────────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """
    Normalize text for stable deduplication.

    Whitespace is collapsed and surrounding whitespace removed.
    Case is normalized because capitalization should not create a
    separate artifact hash.
    """
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_hash(text: str) -> str:
    """Return SHA-256 hash of normalized article text."""
    normalized = _normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _parse_pub_date(entry: dict) -> Optional[datetime]:
    """
    Extract publication time from a feedparser entry.

    Preference:
    1. published_parsed
    2. updated_parsed
    3. published
    4. updated
    """
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)

        if parsed:
            try:
                return datetime(*parsed[:6])
            except (TypeError, ValueError):
                pass

    for field in ("published", "updated"):
        raw = entry.get(field)

        if raw:
            try:
                return datetime.fromisoformat(
                    raw.replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                pass

    return None


def _extract_article_text(url: str) -> Optional[str]:
    """
    Fetch an article URL and extract its main text using trafilatura.

    Returns None when fetching or extraction fails, or when the
    extracted content is too short to be useful.
    """
    try:
        response = requests.get(
            url,
            timeout=HTTP_TIMEOUT_S,
            headers={"User-Agent": USER_AGENT},
            allow_redirects=True,
        )
        response.raise_for_status()

    except requests.RequestException as exc:
        print(f"    [fetch error] {url}: {exc}")
        return None

    extracted = trafilatura.extract(
        response.text,
        include_comments=False,
        include_tables=False,
        favor_precision=True,
        url=url,
    )

    if not extracted or len(extracted.strip()) < MIN_CONTENT_LENGTH:
        print(f"    [skip: too short or empty] {url}")
        return None

    return extracted.strip()


# ── Result type ───────────────────────────────────────────────────────

@dataclass
class CollectionResult:
    source_id: int
    total_feed_entries: int
    new_artifacts: int
    duplicates: int
    fetch_errors: int
    new_artifact_ids: list[int]


# ── Gate 1 ────────────────────────────────────────────────────────────

def collect_from_source(
    source: Source,
    db: Database,
) -> CollectionResult:
    """
    Run Gate 1 for one configured Source.

    Pipeline:

        RSS
          ↓
        article URL
          ↓
        trafilatura
          ↓
        normalized content
          ↓
        SHA-256
          ↓
        Database INSERT
          ↓
        SourceArtifact(status='pending')

    Deduplication is enforced by the database's UNIQUE constraint on
    source_artifacts.content_hash. We deliberately do not require a
    separate db.hash_exists() method.
    """

    if not source.url:
        raise ValueError(
            f"Source '{source.name}' has no URL configured."
        )

    if source.id is None:
        raise ValueError(
            f"Source '{source.name}' has no database ID."
        )

    print(f"\n[Gate 1] Collecting from: {source.name}")
    print(f"         Feed URL: {source.url}")

    # ── Fetch RSS ─────────────────────────────────────────────────────

    feed = feedparser.parse(source.url)

    if getattr(feed, "bozo", False):
        print(
            f"         [feed warning] "
            f"{getattr(feed, 'bozo_exception', 'unknown error')}"
        )

    total_entries = len(feed.entries)

    entries = feed.entries[:MAX_ENTRIES_PER_SOURCE]

    print(
        f"         Found {total_entries} entries, "
        f"processing {len(entries)}"
    )

    new_ids: list[int] = []
    duplicates = 0
    fetch_errors = 0

    # ── Parallel pre-fetch (speed audit 2026-09-04: sequential fetch with a
    # 15s timeout each + 1s politeness sleep dominated Gate 1 wall time while
    # the network sat idle. Fetch+extract are pure network/CPU; DB inserts
    # stay sequential below, so sqlite threading is never touched).
    _links = []
    for _e in entries:
        _l = (_e.get("link", "") or "").strip()
        if _l and _l not in _links:
            _links.append(_l)
    _fetched: dict[str, Optional[str]] = {}
    if _links:
        from concurrent.futures import ThreadPoolExecutor as _Pool
        with _Pool(max_workers=5) as _pool:
            for _l, _t in zip(_links, _pool.map(_extract_article_text, _links)):
                _fetched[_l] = _t

    # ── Process entries ──────────────────────────────────────────────

    for index, entry in enumerate(entries, start=1):

        title = entry.get("title", "").strip()
        link = entry.get("link", "").strip()

        pub_date = _parse_pub_date(entry)

        if not link:
            print(
                f"  [{index}/{len(entries)}] "
                f"No URL in entry '{title[:60]}' — skip"
            )
            fetch_errors += 1
            continue

        print(
            f"  [{index}/{len(entries)}] "
            f"Fetching: {title[:60]}..."
        )

        # ── Extract article (pre-fetched in parallel above) ──────────────

        text = _fetched.get(link)

        if text is None:
            fetch_errors += 1
            continue

        # ── Hash ─────────────────────────────────────────────────────

        content_hash = _content_hash(text)

        # ── Build SourceArtifact ─────────────────────────────────────

        artifact = SourceArtifact(
            source_id=source.id,
            original_title=title,
            original_url=link,
            original_content=text,
            language=source.language,
            media_type="text",
            content_hash=content_hash,
            publication_time=pub_date,
            processing_status=ProcessingStatus.PENDING,
        )

        # ── Insert ───────────────────────────────────────────────────
        #
        # The database defines content_hash as UNIQUE.
        # Therefore the DB itself is the authoritative deduplication
        # mechanism. This also protects against a race condition.

        try:
            artifact_id = db.insert_artifact(artifact)

        except sqlite3.IntegrityError as exc:

            # The expected IntegrityError here is a duplicate
            # content_hash. Do not silently classify every possible
            # integrity error as a duplicate.
            if "content_hash" in str(exc).lower():
                duplicates += 1

                print(
                    "         -> duplicate "
                    "(content_hash already exists), skipping"
                )

                continue

            # Unexpected database integrity error.
            raise

        new_ids.append(artifact_id)

        print(
            f"         -> stored artifact "
            f"id={artifact_id} ({len(text)} chars)"
        )

    # ── Result ───────────────────────────────────────────────────────

    result = CollectionResult(
        source_id=source.id,
        total_feed_entries=total_entries,
        new_artifacts=len(new_ids),
        duplicates=duplicates,
        fetch_errors=fetch_errors,
        new_artifact_ids=new_ids,
    )

    print(
        f"\n[Gate 1] Done: "
        f"{result.new_artifacts} new, "
        f"{result.duplicates} duplicates, "
        f"{result.fetch_errors} errors"
    )

    return result