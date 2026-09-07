"""Step 10 — World Bank evidence adapter (quantitative_official substrate).

Fetches indicator observations from the World Bank Open Data API (free,
keyless) and normalizes them into Measurement-shaped records. This is a
SEPARATE substrate from scholarly literature: WB observations become
Measurements, NEVER studies/findings (Phase 2/3 audit — forcing numeric
official data into `studies` would contaminate the ontology).

Design constraints (Step 10):
- No LLM: indicator + country selection is a deterministic lookup from the
  Investigation Question / Problem entities (Phase 3: retrieval needs no LLM).
- No schema change: insert_measurement already accepts indicator, subject,
  value, unit, reference_period, measurement_source, acquisition_method, quality.
- Distinct failure modes (Phase 9): source-unavailable / no-observation /
  unmappable-question are kept separate; none is faked or collapsed.
- Provenance preserved: indicator code+name, dataset, source org, obs status.
"""
from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

_WB = "https://api.worldbank.org/v2"
_UA = {"User-Agent": "NexusThinkTankAudit/0.1 (mailto:audit@example.com)"}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# Deterministic Question -> indicator map (v0; no LLM). Keyed by substring.
_INDICATOR_MAP = [
    ("trade", "NE.EXP.GNFS.CD", "Exports of goods and services (current US$)"),
    ("export", "NE.EXP.GNFS.CD", "Exports of goods and services (current US$)"),
    ("import", "NE.IMP.GNFS.CD", "Imports of goods and services (current US$)"),
    ("gdp", "NY.GDP.MKTP.CD", "GDP (current US$)"),
    ("inflation", "FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)"),
    ("population", "SP.POP.TOTL", "Population, total"),
    ("life expectancy", "SP.DYN.LE00.IN", "Life expectancy at birth, total (years)"),
    ("co2", "EN.ATM.CO2E.KT", "CO2 emissions (kt)"),
    ("foreign direct investment", "BX.KLT.DINV.CD.WD", "Foreign direct investment, net inflows (BoP, current US$)"),
    ("remittance", "BX.TRF.PWKR.CD.DT", "Personal remittances, received (current US$)"),
    ("oil", "TX.VAL.MRCH.CD.WT", "Fuel exports (current US$)"),
]

# Problem-entity name -> World Bank ISO-2 country code (v0 curated).
_COUNTRY_MAP = {
    "iran": "IR", "afghanistan": "AF", "pakistan": "PK", "turkey": "TR",
    "india": "IN", "china": "CN", "russia": "RU", "united states": "US",
    "israel": "IL", "iraq": "IQ", "saudi arabia": "SA", "turkey": "TR",
    "united arab emirates": "AE", "qatar": "QA", "kuwait": "KW",
    "kazakhstan": "KZ", "turkmenistan": "TM", "azerbaijan": "AZ",
    "armenia": "AM", "georgia": "GE", "oman": "OM",
}


def select_indicator(question_text: str) -> Optional[Dict[str, str]]:
    """Map an Investigation Question to a World Bank indicator (deterministic).

    Returns {code, name} or None if no indicator matches (unmappable question
    -> WB is not the right substrate; caller must NOT fabricate one).
    """
    t = (question_text or "").lower()
    for key, code, name in _INDICATOR_MAP:
        if key in t:
            return {"code": code, "name": name}
    return None


def select_country(entity_names: List[str]) -> Optional[str]:
    """Pick the World Bank ISO-2 country code from a Problem's entities."""
    for e in entity_names or []:
        low = e.strip().lower()
        if low in _COUNTRY_MAP:
            return _COUNTRY_MAP[low]
        for k, v in _COUNTRY_MAP.items():
            if k in low:
                return v
    return None


# ISO-2 -> canonical display name (for measurement subject attribution).
_COUNTRY_DISPLAY = {
    "IR": "Iran", "AF": "Afghanistan", "PK": "Pakistan", "TR": "Türkiye",
    "IN": "India", "CN": "China", "RU": "Russia", "US": "United States",
    "IL": "Israel", "IQ": "Iraq", "SA": "Saudi Arabia", "AE": "UAE",
    "QA": "Qatar", "KW": "Kuwait", "KZ": "Kazakhstan",
    "TM": "Turkmenistan", "AZ": "Azerbaijan", "AM": "Armenia",
    "GE": "Georgia", "OM": "Oman",
}


def country_display_name(iso2: str) -> str:
    """Canonical country display name for a World Bank ISO-2 code."""
    return _COUNTRY_DISPLAY.get((iso2 or "").upper(), iso2 or "Unknown")


def _http_get_json(url: str, timeout: int = 25):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_indicator_observations(country: str, indicator_code: str,
                                 per_page: int = 5) -> Dict:
    """Fetch World Bank observations. Returns a structured result that keeps
    failure modes distinct (Phase 9):

      {'status': 'ok', 'observations': [...]}            # real observations
      {'status': 'no_observation', 'meta': {...}}        # source ok, no rows
      {'status': 'source_unavailable', 'error': str}     # HTTP/timeout/429
    """
    url = (f"{_WB}/country/{country}/indicator/{indicator_code}"
           f"?format=json&per_page={per_page}&date=2015:2024")
    try:
        data = _http_get_json(url)
    except Exception as e:  # noqa: BLE001  (operational failure -> explicit)
        return {"status": "source_unavailable", "error": f"{type(e).__name__}: {e}"}
    if not isinstance(data, list) or len(data) < 2 or not data[1]:
        # data[0] = meta, data[1] = observations list (possibly empty)
        meta = data[0] if isinstance(data, list) and data else {}
        return {"status": "no_observation", "meta": meta}
    obs = [o for o in data[1] if o.get("value") is not None]  # skip missing values
    if not obs:
        return {"status": "no_observation", "meta": data[0]}
    return {"status": "ok", "observations": obs, "meta": data[0]}


def normalize_observation(country: str, indicator: Dict, obs: Dict,
                          subject_entity_name: Optional[str]) -> Dict:
    """Map one WB observation to a Measurement-shaped dict (insert_measurement)."""
    meta = obs.get("indicator") or {}
    return {
        "indicator": indicator["name"],
        "indicator_code": indicator["code"],
        "subject_entity_name": subject_entity_name or country,
        "value": float(obs["value"]),
        "unit": (obs.get("unit") or meta.get("unit") or "").strip() or None,
        "reference_period": str(obs.get("date")),
        "measurement_source": "World Bank WDI",
        "acquisition_method": "worldbank_api",
        "quality": obs.get("obs_status") or None,
    }
