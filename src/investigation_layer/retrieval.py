"""Investigation Layer — OpenAlex retrieval (free, keyless).

Builds a literature query from a Discovery-Layer Problem and fetches
candidate studies. No API key, no cost. Crossref/arXiv are secondary.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

OPENALEX = "https://api.openalex.org/works"
_UA = "nexus-think-tank/1.0 (mailto:research@example.com)"


def _http_get_json(url: str, timeout: int = 25, retries: int = 4) -> dict:
    import time
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            # 429/5xx: OpenAlex throttling -> back off and retry.
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(8.0 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(4.0 * (attempt + 1))
            continue
    raise last


class RetrievalError(RuntimeError):
    """Raised when OpenAlex could not be queried at all (HTTP/network/429),
    as opposed to a clean empty result set. Distinguishing the two is
    epistemically critical: a transport failure is an OPERATIONAL condition
    that must surface as 'deferred', never be silently collapsed into
    'no evidence / no relevant studies'."""


_STRIP_CHARS = '.,;:()"' + "'"


def reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """OpenAlex stores abstracts as a word->positions inverted index."""
    if not inverted_index:
        return ""
    out: dict[int, str] = {}
    for word, positions in inverted_index.items():
        for pos in positions:
            out[pos] = word
    return " ".join(out[i] for i in sorted(out))


# Entities that are too specific to anchor a focused title+abstract search
# (they zero out retrieval). We keep only COUNTRY/REGION-level anchors and drop
# facility / organisation / province-specific names -- the relevance GATE still
# applies the full problem context (incl. Iran-specificity), so recall is served
# by a broad query and precision by the gate (Step 8 evidence-demand audit).
_SPECIFICITY_MARKERS = {
    "hospital", "university", "bank", "ministry", "company", "factory", "plant",
    "facility", "institute", "foundation", "association", "council", "agency",
    "department", "bureau", "corporation", "province", "red crescent",
    "organization", "organisation", "authority", "committee", "parliament",
}


def _is_query_safe_entity(name: str) -> bool:
    """True if the entity is a broad enough anchor for a focused search."""
    n = name.strip()
    low = n.lower()
    if any(m in low for m in _SPECIFICITY_MARKERS):
        return False
    # keep short, country/region-like anchors (1-2 tokens); drop long specifics
    if len(n.split()) > 2:
        return False
    return True


def build_query(problem: dict) -> str:
    """Compose a COHERENT OpenAlex query phrase from a Problem.

    Step 7/7b finding: retrieval recall for the pinned (geopolitical) problems
    was near-zero because (a) we used the broad full-text `search=` operator and
    (b) build_query emitted a KEYWORD SALAD (topic + 6 statement words + entities)
    that the focused `title_and_abstract.search:` filter cannot match. The
    Investigation Questions Gate 2 generates ARE coherent investigative phrases
    ("what specific medical facilities were damaged in Khuzestan?") -- the right
    source for a focused query. So we now prefer a question-derived phrase, with
    the most specific entity names appended for grounding. Capped ~140 chars.

    problem: dict with 'statement', 'topic', 'question_texts' (list),
    'entity_names' (list).
    """
    stop = {"the", "a", "an", "and", "or", "of", "in", "on", "to", "for", "with",
            "is", "are", "was", "were", "be", "by", "at", "as", "that", "this",
            "from", "into", "what", "which", "how", "who", "their", "its", "our",
            "specific", "total", "precise", "ongoing", "systematic", "between",
            "affect", "affects", "effect", "effects"}  # linking verbs are not topic terms
    # 1) Coherent phrase from the top-ranked Investigation Question (strip lead
    #    interrogatives so it reads as a topic phrase, not a question). Step 16:
    #    the prior list missed "does/do/did/is/are" -- a Question like "How does
    #    the concentration ... affect ..." kept the leading "does", producing a
    #    polluted phrase ("does concentration military bases ...") that
    #    title_and_abstract.search: matches on ZERO papers -> guaranteed
    #    no_evidence. Strip ALL leading interrogatives/auxiliaries.
    phrase_words: list[str] = []
    for q in problem.get("question_texts", []) or []:
        # S3: Persian questions (our desk drafts FA) contribute no Latin tokens
        # that OpenAlex can match -- skip them so the EN statement fallback
        # fires (live P22: Persian phrase retrieved 0 studies). Latin questions
        # keep the preferred question-derived behavior.
        if len(re.findall(r"[a-zA-Z]", q)) < 4:
            continue
        q = q.lower().replace("?", " ")
        for lead in ("what specific ", "what ", "how ", "which ", "who ",
                     "why ", "when ", "where ", "does ", "do ", "did ",
                     "is ", "are ", "can ", "could ", "would ", "will "):
            while q.startswith(lead):
                q = q[len(lead):]
        for w in q.replace(",", " ").split():
            w = w.strip(_STRIP_CHARS)
            if len(w) > 3 and w not in stop and w not in phrase_words:
                phrase_words.append(w)
        if len(phrase_words) >= 6:
            break
    # 2) Fallback: salient statement nouns if no questions supplied a phrase.
    if not phrase_words:
        stmt = (problem.get("statement") or "").lower()
        for w in stmt.replace("/", " ").split():
            w = w.strip(_STRIP_CHARS)
            if len(w) > 3 and w not in stop and w not in phrase_words:
                phrase_words.append(w)
            if len(phrase_words) >= 6:
                break
    # 3) Entity grounding is NOT appended to the retrieval query. Step 16 audit
    #    proved entity anchors (even country-level "Iran") over-constrain
    #    title_and_abstract.search: (AND of all terms) and collapse recall to 0
    #    for topics whose literature is general rather than geo-tagged:
    #    "military bases missile vulnerability" -> 8 results, but
    #    "...vulnerability missiles Iran" -> 0. Geographic relevance is a GATE
    #    concern, not a retrieval-filter concern: the gate receives entity_names
    #    (classify_relevance) and judges Iran-specificity there. Keeping the
    #    retrieval query free of entity terms maximizes recall; the gate restores
    #    precision. (Step 8 reached the same conclusion for facility/province
    #    names; Step 16 extends it to country-level anchors.)
    seen = set()
    out = []
    for k in phrase_words:
        if k.casefold() not in seen:
            seen.add(k.casefold())
            out.append(k)
    return " ".join(out)[:140]


def fetch_studies(query: str, per_page: int = 5, only_oa: bool = True) -> list[dict]:
    """Return raw OpenAlex work records for a query.

    Query OPERATOR matters more than wording (Step 7b experiment): a focused
    `title_and_abstract.search:` FILTER recovers relevant literature that the
    broad full-text `search=` scatters away (e.g. healthcare-attacks problem:
    0/6 with search= vs 6/6 with the filter). So we try, in order:
      1) filter=title_and_abstract.search:<q>  (focused, primary)
      2) search=<q>                            (broad fallback)
      3) search=<short q>                      (shortened fallback, avoids 400)
    The 429/timeout-resilient relevance gate (pipeline) filters precision; this
    function only improves the candidate pool's recall. No auth/key needed.

    FAILURE SEMANTICS (epistemic-critical, Step 12.5):
      - Clean HTTP 200 with an empty result set  -> return []  (genuine absence)
      - Transport / HTTP error on EVERY attempt  -> raise RetrievalError
        (operational failure: 429/timeout/network). The pipeline must turn this
        into a 'deferred' Question, NEVER into 'no_evidence'. Swallowing the
        error and returning [] would let an API outage masquerade as "no
        relevant literature exists".
    """
    sel = ("id,title,abstract_inverted_index,authorships,primary_location,"
           "open_access,type,publication_year,cited_by_count,concepts,doi")
    # Quote the phrase for the focused title_and_abstract.search: filter.
    # OpenAlex REQUIRES a quoted phrase ("iranian pistachio exports"); an
    # unquoted multi-word query is silently ignored and returns the default
    # most-cited OA set (Step 31: ResNet/DFT/protein-assay noise). Quoting makes
    # the focused branch return genuinely topically-relevant literature.
    q_phrase = urllib.parse.quote('"' + query + '"')
    q_bare = urllib.parse.quote(query)

    last_error: BaseException | None = None

    def _attempt(url: str) -> list[dict] | None:
        """Return results on a clean HTTP success (even if empty); raise/record
        on transport/HTTP error; None if the caller should continue to fallback."""
        nonlocal last_error
        try:
            return _http_get_json(url).get("results", [])
        except (urllib.error.URLError, urllib.error.HTTPError,
                TimeoutError, json.JSONDecodeError) as e:
            last_error = e
            return None  # signal: errored, try next fallback

    # Detect OpenAlex's "default popular set" response: when a filter is silently
    # ignored, OpenAlex returns the highest-cited works regardless of query. We
    # treat a TAS branch that returns 0 results as "no candidate pool" and fall
    # through to the broad search (which is tolerant of wording).
    # 1) focused title+abstract phrase filter (primary, quoted)
    url_tas = f"{OPENALEX}?filter=title_and_abstract.search:{q_phrase}&per_page={per_page}&select={sel}"
    res = _attempt(url_tas)
    if res is not None and len(res) > 0:
        return res

    # 2) broad full-text search fallback (tolerant of wording); apply OA pref here
    url_search = f"{OPENALEX}?search={q_bare}&per_page={per_page}&select={sel}"
    if only_oa:
        url_search += "&filter=is_oa:true"
    res = _attempt(url_search)
    if res is not None:
        return res
    res = _attempt(url_search.replace("&filter=is_oa:true", ""))
    if res is not None:
        return res

    # 3) shortened broad search fallback
    short = query[:80]
    res = _attempt(
        f"{OPENALEX}?search={urllib.parse.quote(short)}&per_page={per_page}&select={sel}"
    )
    if res is not None:
        return res

    # Every attempt errored (429/timeout/network) -> operational failure.
    raise RetrievalError(
        f"OpenAlex unreachable for query {query!r}: "
        f"{type(last_error).__name__}: {last_error}"
    )


def normalize_study(work: dict) -> dict:
    """Flatten an OpenAlex work record into our storage shape."""
    auth = work.get("authorships", []) or []
    authors = [a.get("raw_author_name") or a.get("author", {}).get("display_name")
               for a in auth if (a.get("raw_author_name") or a.get("author", {}).get("display_name"))]
    institutions = []
    countries = []
    for a in auth:
        for inst in a.get("institutions", []) or []:
            nm = inst.get("display_name")
            if nm and nm not in institutions:
                institutions.append(nm)
        for c in a.get("countries", []) or []:
            if c and c not in countries:
                countries.append(c)
    concepts = [c.get("display_name") for c in work.get("concepts", []) or []]
    loc = work.get("primary_location") or {}
    oa = work.get("open_access", {}) or {}
    return {
        "source_work_id": work.get("id"),
        "title": work.get("title") or "",
        "year": work.get("publication_year"),
        "study_type": work.get("type"),
        "authors": authors,
        "institutions": institutions,
        "countries": countries,
        "doi": loc.get("landing_page_url") if loc else None,
        "landing_url": loc.get("landing_page_url") if loc else None,
        "pdf_url": oa.get("oa_url"),
        "abstract": reconstruct_abstract(work.get("abstract_inverted_index")),
        "concepts": concepts,
        "cited_by_count": work.get("cited_by_count", 0),
    }


# ---------------------------------------------------------------------------
# Secondary source: Semantic Scholar (free, keyless, polite-pool via mailto).
# Added for V0.1 resilience: OpenAlex rate-limits our IP hard, so a second
# source both improves recall and de-risks retrieval when OpenAlex 429s.
# ---------------------------------------------------------------------------
S2 = "https://api.semanticscholar.org/graph/v1/paper/search"
_S2_FIELDS = ("title,abstract,year,authors,externalIds,publicationVenue,"
              "citationCount,openAccessPdf")


def fetch_studies_s2(query: str, per_page: int = 6) -> list[dict]:
    """Semantic Scholar keyless search -> normalized study dicts.

    S2 free tier is unauthenticated-limited; retry on 429/5xx with backoff and
    raise RetrievalError if it stays down, so the caller treats it as an
    OPERATIONAL condition, never 'no evidence'.
    """
    url = (f"{S2}?query={urllib.parse.quote(query)}&limit={per_page}"
           f"&fields={_S2_FIELDS}&mailto=research@example.com")
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(6.0 * (attempt + 1))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(4.0 * (attempt + 1))
            continue
    else:
        raise RetrievalError(
            f"Semantic Scholar unreachable for query {query!r}: {last}")
    out = []
    for p in data.get("data", []):
        ns = normalize_study_s2(p)
        if ns["title"] and ns["abstract"]:
            out.append(ns)
    return out


def normalize_study_s2(p: dict) -> dict:
    """Map a Semantic Scholar paper record into our storage shape."""
    authors = []
    for a in p.get("authors", []) or []:
        nm = a.get("name")
        if nm and nm not in authors:
            authors.append(nm)
    ext = p.get("externalIds", {}) or {}
    doi = ext.get("DOI")
    oa = p.get("openAccessPdf", {}) or {}
    venue = (p.get("publicationVenue", {}) or {}).get("name")
    abstract = (p.get("abstract") or "").strip()
    return {
        "source_work_id": doi or f"s2:{p.get('paperId')}",
        "title": p.get("title") or "",
        "year": p.get("year"),
        "study_type": "journal" if venue else "preprint",
        "authors": authors,
        "institutions": [],
        "countries": [],
        "doi": doi,
        "landing_url": f"https://doi.org/{doi}" if doi else None,
        "pdf_url": oa.get("url"),
        "abstract": abstract,
        "concepts": [],
        "cited_by_count": p.get("citationCount", 0) or 0,
        "source": "semantic_scholar",
    }


if __name__ == "__main__":
    q = build_query({"statement": "groundwater depletion", "topic": "water",
                     "question_texts": ["interventions that worked elsewhere"],
                     "entity_names": ["Iran"]})
    print("query:", q)
    for w in fetch_studies(q, per_page=2):
        s = normalize_study(w)
        print("-", s["title"][:60], "| abstract len", len(s["abstract"]))
