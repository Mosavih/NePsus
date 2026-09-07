"""V0.1 -- live news context layer (the 'what is happening NOW' sense).

Statistical sources tell us where Iran WAS; for a country in crisis the gap between
the newest observation and today can contain the single most important event (war,
sanctions, escalation). A pipeline blind to that writes posts that are numerically
correct and topically dead -- e.g. analyzing 'conflict risk' with data ending
before the conflict.

This module pulls open RSS/Atom feeds (free, ToS-clean: RSS exists to be consumed),
scores items against a problem's statement, and returns a compact, dated context
block. STRICT ROLE SEPARATION:
  - statistical measurements  => numeric evidence (what the data shows)
  - news items                => contemporary framing only (what is happening,
                                 attributed, never silently merged into data)

Everything is attribution-preserving: outlet, date, title. No scraping of article
bodies (headlines+dates are sufficient for framing and are the least fragile).
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "NexusThinkTank/0.1 (mailto:research@example.com)"

# Open feeds verified reachable from this environment (2026-08 probe;
# Guardian Technology added 2026-09-04 after user-ask questions on
# online business/digital ads found zero tech coverage in the corpus).
FEEDS = {
    "The Guardian": "https://www.theguardian.com/world/iran/rss",
    "Al Jazeera":   "https://www.aljazeera.com/xml/rss/all.xml",
    "BBC News":     "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "UN News":      "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    "Guardian Tech": "https://www.theguardian.com/uk/technology/rss",
    "BBC Tech": "http://feeds.bbci.co.uk/news/technology/rss.xml",
    "Guardian Science": "https://www.theguardian.com/science/rss",
    "Guardian Environment": "https://www.theguardian.com/environment/rss",
}

_STOP = set("""a an and are as at be by for from has have in into is it its of on or
that the their this to was were will with would iran iranian over after amid say says
new us uk un""".split())

_cache = {"t": 0.0, "items": []}


def _fetch_feed(url: str, timeout: int = 15) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=timeout, context=CTX).read()
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        if not title:
            continue
        dt = None
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
            try:
                dt = datetime.strptime(pub.replace("GMT", "UTC"), fmt)
                break
            except ValueError:
                continue
        out.append({"title": title, "url": link, "published": pub,
                    "dt": dt, "outlet": None})
    # Atom fallback
    if not out:
        ns = "{http://www.w3.org/2005/Atom}"
        for en in root.iter(f"{ns}entry"):
            title = (en.findtext(f"{ns}title") or "").strip()
            link_el = en.find(f"{ns}link")
            link = link_el.get("href") if link_el is not None else ""
            pub = (en.findtext(f"{ns}updated") or en.findtext(f"{ns}published") or "")
            if title:
                out.append({"title": title, "url": link, "published": pub,
                            "dt": None, "outlet": None})
    return out


def fetch_news(max_age_days: int = 21, force: bool = False) -> list[dict]:
    """All recent items across FEEDS (cached ~30min per process)."""
    if not force and _cache["items"] and time.time() - _cache["t"] < 1800:
        return _cache["items"]
    items = []
    for outlet, url in FEEDS.items():
        try:
            got = _fetch_feed(url)
            for g in got:
                g["outlet"] = outlet
            items.extend(got)
        except Exception:
            continue  # one feed down != no news; degrade gracefully
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    fresh = []
    for it in items:
        dt = it.get("dt")
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < cutoff:
                continue
            it["dt"] = dt
        fresh.append(it)
    fresh.sort(key=lambda x: x.get("dt") or datetime.min.replace(tzinfo=timezone.utc),
               reverse=True)
    _cache["t"] = time.time()
    _cache["items"] = fresh
    return fresh


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", text.lower())} - _STOP


def context_for(statement: str, iran_note: str = "", max_items: int = 6,
                min_score: int = 2) -> str:
    """Compact Persian-ready news block matched to a problem, or '' if nothing
    relevant. Deterministic token-overlap scoring (explainable)."""
    items = fetch_news()
    if not items:
        return ""
    q = _tokens(statement + " " + (iran_note or ""))
    scored = []
    for it in items:
        t = _tokens(it["title"])
        overlap = q & t
        score = len(overlap) + (2 if "iran" in it["title"].lower() else 0)
        if score >= min_score:
            scored.append((score, it))
    scored.sort(key=lambda x: (-x[0], x[1].get("dt") or
                               datetime.min.replace(tzinfo=timezone.utc)), reverse=False)
    scored.sort(key=lambda x: (-x[0],))
    lines = []
    today = datetime.now(timezone.utc).date().isoformat()
    lines.append(f"TODAY: {today}")
    lines.append("NEWS CONTEXT (خبرهای روزِ مرتبط — فقط برای پیوند زمانی و قلاب;")
    lines.append("اینها داده آماری نیستند و نباید به‌عنوان شاخص اقتصادی/آماری نقل شوند):")
    for score, it in scored[:max_items]:
        d = it["dt"].strftime("%Y-%m-%d") if it.get("dt") else it.get("published", "")[:10]
        lines.append(f"- [{d}] ({it['outlet']}) {it['title']}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(context_for(
        "Hostage to broader conflict risks, local communities and European allies "
        "face security threats tied to host-country support for military operations "
        "against Iran; NATO credibility questioned",
        "war security sanctions conflict"))
