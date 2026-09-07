"""T8 -- LLM query expansion + multi-query investigation.

The deterministic build_query() strips stop-words from the problem statement and
produces mediocre scholarly queries ("high vacancy rates resource waste towns").
This module asks a fast LLM to translate each Problem into 3-5 ENGLISH
scholarly search queries that the actual literature would answer, runs them
all through OpenAlex, dedupes by work id, and hands the union to
investigate_problem's frozen mode.
"""
from __future__ import annotations

import re

from src.investigation_layer.extraction import _client


def _expand_llm(client, usr, model):
    import time
    last = None
    for attempt in range(3):
        try:
            return client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": usr}],
                temperature=0.3,
            )
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            if "quota" in msg.lower() and "429" in msg:
                break  # daily wall -> caller falls back to next model
            if any(t in msg for t in ("429", "503", "502", "timeout",
                                      "Connection", "RateLimit")):
                time.sleep(6.0 * (attempt + 1))
                continue
            raise
    raise last


def expand_queries(problem_statement: str, n: int = 4,
                   model: str | None = None) -> list:
    from src.route_health import chat as _rchat
    usr = (
        f"Problem: {problem_statement}\n\n"
        f"Write {n} different ENGLISH search queries for finding ACADEMIC "
        f"papers that would provide EVIDENCE about this problem (causes, "
        f"effects, interventions). One query per line, no numbering, no "
        f"explanation. Each 3-8 words, using standard scholarly terminology "
        f"(e.g. 'housing vacancy shrinkage' not 'empty homes problem')."
    )
    # Resilient: assigned combo first, siblings as fallback (health ledger).
    out, _used = _rchat("expand",
                        [{"role": "user", "content": usr}],
                        temperature=0.3, model=model)
    qs = []
    for line in out.splitlines():
        line = line.strip().lstrip("-•*0123456789. )")
        if 10 <= len(line) <= 90 and " " in line:
            qs.append(line)
    return qs[:n]


def fetch_multi_query(queries: list, per_query: int = 6) -> dict:
    """Run several queries across OpenAlex (primary) AND Semantic Scholar
    (secondary). Dedupe by normalized id/title. Either source failing is an
    operational condition, not 'no evidence' -- we collect both and report errors.
    """
    from src.investigation_layer.retrieval import fetch_studies, fetch_studies_s2
    seen = {}
    errors = []

    def _key(w):
        doi = w.get("doi")
        if doi:
            return ("doi", doi.lower())
        t = (w.get("title") or "").strip().lower()
        return ("title", t[:80]) if t else None

    def _add(works):
        for w in works:
            k = _key(w)
            if k and k not in seen:
                seen[k] = w

    for q in queries:
        try:
            _add(fetch_studies(q, per_page=per_query, only_oa=True))
        except Exception as e:  # noqa: BLE001
            errors.append(f"[openalex] {q}: {e}")
        try:
            _add(fetch_studies_s2(q, per_page=per_query))
        except Exception as e:  # noqa: BLE001
            errors.append(f"[s2] {q}: {e}")
    return {"works": list(seen.values()), "queries": queries, "errors": errors}
