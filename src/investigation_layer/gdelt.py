"""V0.1 -- GDELT analysis/forecast probe (case-1 sources for post-war impact).

GDELT DOC 2.0 API: free, keyless, full-text news search across global media.
Used with targeted queries (forecast / outlook / impact / estimate) it surfaces
*analytical* coverage -- IMF/World Bank outlooks, war-economy assessments --
that RSS headline feeds miss entirely.

Rate limits: be polite (>=15s between calls), cache per-process, degrade to None
on repeated 429 (caller then falls back to case-2 discard or case-3 own forecast).
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import ssl
from datetime import datetime, timezone

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "NexusThinkTank/0.1 (mailto:research@example.com)"

_last_call = [0.0]
_cache: dict[str, tuple[float, list]] = {}
MIN_GAP_S = 16.0          # GDELT DOC is strict about bursts
CACHE_TTL_S = 3600.0

# English-language scope keeps titles parseable; quality outlets preferred via
# domain filters rather than hardcoded allowlist (sourcecountry too coarse).
# Generic fallback queries (used only when problem-specific ones yield nothing).
_QUERY_TEMPLATES = [
    'iran economy forecast "{yr}"',
    'iran economy outlook war',
]

# V0.2: domain keyword pools. Problem-specific queries are built from the
# tokens the problem actually contains, so a housing problem searches
# construction/materials analysis -- not the same "iran economy outlook" every
# post used in V0.1 (the monotony defect).
_DOMAIN_POOLS = {
    "housing":     ['iran housing construction {kw}', 'iran urban development {kw}'],
    "construction":['iran construction materials {kw}', 'iran building sector {kw}'],
    "energy":      ['iran energy electricity {kw}', 'iran water scarcity {kw}'],
    "water":       ['iran water drought {kw}', 'iran agriculture water {kw}'],
    "export":      ['iran exports {kw}', 'iran trade sanctions {kw}'],
    "agriculture": ['iran agriculture pistachio {kw}', 'iran farming export {kw}'],
    "women":       ['iran women employment labor {kw}', 'iran women workforce {kw}'],
    "labor":       ['iran unemployment labor market {kw}', 'iran jobs economy {kw}'],
    "cooperatives":['iran small business cooperatives {kw}', 'iran local economy {kw}'],
    "conflict":    ['iran war economic impact {kw}', 'iran conflict reconstruction {kw}'],
    "sanctions":   ['iran sanctions effect {kw}', 'iran oil sanctions economy {kw}'],
    "family":      ['iran marriage family economy {kw}', 'iran youth household {kw}'],
    "migration":   ['iran brain drain migration {kw}', 'iran emigration {kw}'],
}
_KW_FILL = {"housing":"outlook", "construction":"demand", "energy":"outlook",
            "water":"crisis", "export":"outlook", "agriculture":"outlook",
            "women":"outlook", "labor":"outlook", "cooperatives":"role",
            "conflict":"outlook", "sanctions":"impact", "family":"economics",
            "migration":"trend"}


def _detect_domains(statement: str) -> list[str]:
    s = statement.lower()
    hits = []
    for dom, words in {
        "housing": ("housing", "home", "vacanc", "mehr", "residential"),
        "construction": ("construct", "build"),
        "energy": ("energy", "electric", "gas", "power grid"),
        "water": ("water", "drought", "scarcity"),
        "export": ("export",),
        "agriculture": ("agricultur", "pistachio", "farm", "crop"),
        "women": ("women", "gender", "female"),
        "labor": ("labor", "employment", "unemploy", "job", "participation"),
        "cooperatives": ("cooperative",),
        "conflict": ("conflict", "war", "military", "security", "nato"),
        "sanctions": ("sanction",),
        "family": ("marriage", "family formation", "household formation", "divorce"),
        "migration": ("migration", "brain drain", "emigrat"),
    }.items():
        if any(w in s for w in words):
            hits.append(dom)
    return hits or ["conflict"]  # macro fallback keeps old behavior


def build_problem_queries(statement: str) -> list[str]:
    """Problem-specific GDELT queries (V0.2 anti-monotony)."""
    doms = _detect_domains(statement)
    out = []
    for dom in doms[:3]:
        for t in _DOMAIN_POOLS.get(dom, [])[:1]:
            out.append(t.format(kw=_KW_FILL.get(dom, "outlook")))
    return out

# Domains that are primary analytical sources vs aggregators/wire repeats.
_PREFERRED = ("imf.org", "worldbank.org", "ft.com", "economist.com", "reuters.com",
              "bloomberg.com", "apnews.com", "wsj.com", "ft.com", "cnbc.com",
              "aljazeera.com", "theguardian.com", "nytimes.com", "dw.com",
              "rfi.fr", "france24.com", "bbc.co.uk", "bbc.com")


def _polite_get(url: str) -> dict:
    wait = _last_call[0] + MIN_GAP_S - time.time()
    if wait > 0:
        time.sleep(wait)
    last_err = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            raw = urllib.request.urlopen(req, timeout=25, context=CTX).read()
            _last_call[0] = time.time()
            return json.loads(raw)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(12 * (attempt + 1))
    raise RuntimeError(f"gdelt unreachable: {last_err}")


_DISK_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "var", "gdelt_cache.json")


def _disk_load() -> dict:
    try:
        with open(_DISK_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_save(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_DISK_CACHE), exist_ok=True)
        with open(_DISK_CACHE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def search_analysis(query: str, timespan: str = "6m", maxrecords: int = 20) -> list[dict]:
    """Run one GDELT DOC query; returns normalized articles.

    Caching is two-tier: in-process dict + on-disk JSON shared by consecutive
    runs, so a batch of problems costs at most one GDELT round per query per
    hour instead of one per problem (GDELT 429s bursts from one IP).
    """
    key = f"{query}|{timespan}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    disk = _disk_load()
    ts, blob = disk.get(key, (0, None))
    if blob and time.time() - ts < CACHE_TTL_S:
        _cache[key] = (ts, blob)
        return blob
    url = ("https://api.gdeltproject.org/api/v2/doc/doc?query="
           + urllib.parse.quote(query)
           + f"&mode=artlist&maxrecords={maxrecords}&format=json&timespan={timespan}")
    try:
        data = _polite_get(url)
    except RuntimeError:
        return []          # caller decides fallback; never fabricate
    arts = []
    for a in data.get("articles", []):
        title = a.get("title") or ""
        if not title:
            continue
        sd = a.get("seendate") or ""
        try:
            dt = datetime.strptime(sd, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
        dom = (a.get("domain") or "").lower()
        arts.append({
            "title": title.strip(),
            "url": a.get("url"),
            "domain": dom,
            "outlet": dom,
            "dt": dt,
            "seen": sd,
            "preferred": any(dom.endswith(p) or p in dom for p in _PREFERRED),
            "language": a.get("language"),
        })
    # English first, preferred domains first, newest first
    arts.sort(key=lambda x: (not x["preferred"], x["language"] not in ("English", "eng"),
                             -(x["dt"].timestamp() if x["dt"] else 0)))
    _cache[key] = (time.time(), arts)
    disk = _disk_load()
    disk[key] = [time.time(), arts]
    _disk_save(disk)
    return arts


def find_post_war_analysis(max_items: int = 5, statement: str | None = None) -> list[dict]:
    """Case-1 source hunt: post-war (current-year) analyses of Iran.
    With `statement`, runs problem-specific queries FIRST (V0.2), then falls
    back to generic economy ones only if specific yields too little."""
    yr = datetime.now(timezone.utc).year
    seen_titles: set[str] = set()
    out: list[dict] = []
    templates: list = []
    if statement:
        templates.extend(build_problem_queries(statement)[:3])
    templates.extend(_QUERY_TEMPLATES[:1])
    for tmpl in templates:
        q = tmpl.format(yr=yr)
        try:
            arts = search_analysis(q)
        except Exception:
            continue
        for a in arts:
            stem = " ".join((a["title"] or "").lower().split()[:8])
            if stem in seen_titles:
                continue
            seen_titles.add(stem)
            out.append(a)
        if len(out) >= max_items * 3:
            break
    out.sort(key=lambda x: (not x["preferred"],
                            -(x["dt"].timestamp() if x["dt"] else 0)))
    return out[:max_items]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, ".")
    for a in find_post_war_analysis():
        d = a["dt"].strftime("%Y-%m-%d") if a["dt"] else a["seen"][:8]
        star = "*" if a["preferred"] else " "
        print(f"{star} [{d}] ({a['domain']}) {a['title'][:95]}")
