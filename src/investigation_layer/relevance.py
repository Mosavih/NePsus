"""V0.1 -- three-case relevance gate + coherent narrative composition.

Implements the user's decision fork for contemporary relevance:
  case 1: post-war (current-year) analysis/forecast found in open sources
          -> include it as ANALYSIS CONTEXT, compose the post around it
  case 2: no such analysis found AND no news bridge -> DISCARD the problem
          (a numbers-only post about a pre-crisis snapshot is not interesting
          enough to publish; honesty over output volume)
  case 3: no external analysis but we hold real measured data -> the composer
          produces an explicit, clearly-labeled OWN FORECAST section
          (transparent reasoning from the data, never invented numbers)

Plus narrative cohesion (user critique #2): posts were fragmented into sections
that don't flow. The composer now gets a single-spine requirement and a
no-section-header rule for data_spotlight/listicle formats; reviewer scores
cohesion and it gates PASS.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


# --------------------------------------------------------------------------
# Case resolution
# --------------------------------------------------------------------------

def resolve_case(dossier: dict) -> dict:
    """Decide case 1/2/3 for a dossier.

    Returns {case: 'analysis'|'discard'|'own_forecast', analysis_items, reason}.
    """
    yr = datetime.now(timezone.utc).year
    analysis = []
    try:
        from src.investigation_layer.gdelt import find_post_war_analysis
        candidates = find_post_war_analysis(
            statement=dossier.get("statement") or "")
    except Exception:
        candidates = []
    pat = re.compile(
        r"\b(forecast|outlook|projection|estimate|impact|analysis|gdp|inflation"
        r"|economy|economic|sanctions)\b", re.I)
    for a in candidates:
        if not pat.search(a.get("title") or ""):
            continue
        if a.get("dt") and a["dt"].year < yr - 1:
            continue                      # too old to bear on the current war
        analysis.append(a)
        if len(analysis) >= 4:
            break

    has_news = bool(dossier.get("news_context"))
    has_data = bool(dossier.get("coverage", {}).get("has_evidence"))

    if analysis:
        return {"case": "analysis", "analysis_items": analysis,
                "statement": dossier.get("statement") or "",
                "reason": f"{len(analysis)} post-war analysis item(s) found"}
    if has_news and has_data:
        # News bridge exists (war/sanctions headlines) but no numeric analysis:
        # own-forecast path is still allowed and the news gives the hook.
        return {"case": "own_forecast", "analysis_items": [],
                "reason": "no external analysis; news hook + own data"}
    if has_data:
        return {"case": "own_forecast", "analysis_items": [],
                "reason": "no external analysis; data only"}
    return {"case": "discard", "analysis_items": [],
            "reason": "no post-war analysis found and no evidence base"}


ANALYSIS_BLOCK_HEADER = (
    "ANALYSIS CONTEXT (تحلیل‌های پس از بحران از منابع باز — برای روایتِ امروز؛\n"
    "اینها داده آماری رسمی نیستند مگر عدد صریح داخل تیتر باشد و با ذکر منبع نقل شود):"
)


def format_analysis_block(items: list[dict]) -> str:
    if not items:
        return ""
    lines = [ANALYSIS_BLOCK_HEADER]
    for a in items:
        d = a["dt"].strftime("%Y-%m-%d") if a.get("dt") else (a.get("seen") or "")[:8]
        lines.append(f"- [{d}] ({a.get('domain','')}) {a['title']}")
    return "\n".join(lines)
