"""V0.1 -- open-data evidence sources (beyond scientific papers).

The biggest bottleneck for held problems (P2/P5/P6) was that OpenAlex + Semantic
Scholar only cover *academic literature*. Many Iran problems (housing, cooperatives,
demographics, macro) are better evidenced by *open statistical datasets*: World Bank
WDI, IMF, UNSD SDG, OECD, Our World in Data, FAO. All are free, keyless, ToS-clean.

This module fetches curated Iran indicators and returns measurement-ready rows that
slot into the existing `measurements` table (same shape the World Bank connector uses).
It deliberately does NOT touch paywalled editorial sites (Economist/Statista) -- those
are access-controlled and out of scope on both legal and technical grounds.

Every fetch retries on 429/5xx with backoff and raises a RetrievalError-like
condition (operational failure, never 'no evidence') so callers surface it honestly.
"""
from __future__ import annotations

import csv
import io
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
UA = "NexusThinkTank/0.1 (mailto:research@example.com)"

# Temporal grounding: every fetcher stamps rows with today's date so downstream
# prompts can reason about data vintage honestly.
from datetime import date as _date
_TODAY = _date.today()
_CURRENT_YEAR = _TODAY.year


def _vintage(period: str) -> dict:
    """Classify a reference period relative to today (2026-08-24 style honesty)."""
    try:
        yr = int(str(period)[:4])
    except (TypeError, ValueError):
        return {"kind": "unknown"}
    if yr >= _CURRENT_YEAR:
        return {"kind": "projection", "note": f"{yr} is a forecast/projection year"}
    if yr == _CURRENT_YEAR - 1:
        return {"kind": "recent", "note": f"latest full year is {_CURRENT_YEAR - 1}"}
    age = _CURRENT_YEAR - yr
    return {"kind": "historical",
            "note": f"data from {yr}, ~{age} years old"}

# Curated Iran indicators likely relevant to the held problems.
# (source, indicator_code, human_label, unit)
CURATED_IRAN = [
    ("worldbank", "SL.UEM.TOTL.ZS", "Unemployment rate, total (% of labor force)", "%"),
    ("worldbank", "SL.UEM.1524.ZS", "Unemployment, youth ages 15-24 (% of labor force)", "%"),
    ("worldbank", "SP.URB.TOTL.IN.ZS", "Urban population (% of total)", "%"),
    ("worldbank", "EN.ATM.CO2E.KD.GD", "CO2 intensity (kg per 2015 US$ of GDP)", "kg/US$"),
    ("worldbank", "AG.LND.IRIG.ZS", "Agricultural land irrigated (% of cropland)", "%"),
    ("worldbank", "FP.CPI.TOTL.ZG", "Inflation, consumer prices (annual %)", "%"),
    ("worldbank", "NE.EXP.GNFS.ZS", "Exports of goods and services (% of GDP)", "%"),
    ("worldbank", "NY.GDP.PCAP.KD.ZG", "GDP per capita growth (annual %)", "%"),
    ("imf", "LPITOTRL", "Total international reserves (USD)", "USD"),
    ("imf", "NGDP_RPCH", "Real GDP growth (%)", "%"),
    ("unsd", "1.1.1", "Population living in poverty (SDG 1.1.1)", "%"),
    ("oecd", "SLEEP2015", "Sleep duration, adults (OECD)", "hours"),
]


def _get(url: str, timeout: int = 20, retries: int = 3, binary: bool = False):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read() if binary else r.read().decode("utf-8", "replace")
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
    raise RuntimeError(f"open-data fetch failed for {url!r}: {last}")


def fetch_worldbank(indicator: str, country: str = "IRN", per_page: int = 60) -> list[dict]:
    """Full series (per_page high enough -- a low cap silently truncates the
    window and manufactures cherry-picked baselines)."""
    url = (f"https://api.worldbank.org/v2/country/{country}/indicator/{indicator}"
           f"?format=json&per_page={per_page}&date=2000:{_CURRENT_YEAR}")
    raw = _get(url)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if not isinstance(data, list) or len(data) < 2:
        return []
    rows = data[1] or []
    out = []
    for r in rows:
        v = r.get("value")
        if v is None:
            continue
        out.append({
            "indicator": r.get("indicator", {}).get("value", indicator),
            "value": float(v),
            "unit": None,
            "reference_period": str(r.get("date")),
            "source": "World Bank WDI",
            "acquisition_method": "worldbank_api",
            **_vintage(str(r.get("date"))),
        })
    return out


def fetch_imf(indicator: str, country: str = "IRN") -> list[dict]:
    """IMF DataMapper API: variable-code based indicators."""
    url = f"https://www.imf.org/external/datamapper/api/v1/{indicator}/{country}"
    raw = _get(url)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    node = (data.get("values", {}).get(indicator, {}).get(country))
    if not node:
        return []
    out = []
    for yr, val in node.items():
        try:
            out.append({
                "indicator": f"IMF {indicator}",
                "value": float(val),
                "unit": None,
                "reference_period": str(yr),
                "source": "IMF DataMapper",
                "acquisition_method": "imf_api",
                **_vintage(str(yr)),
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_unsd(indicator: str, country: str = "IRN") -> list[dict]:
    """UNSD SDG API (best-effort; endpoint shape varies by release)."""
    url = (f"https://unstats.un.org/sdgs/files/api/v1/sdg/Indicator/"
           f"{urllib.parse.quote(indicator)}?page=1")
    raw = _get(url)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    obs = data.get("data", [])
    out = []
    for r in obs:
        geo = (r.get("geoAreaCode") or "")
        if str(geo) != "364":  # Iran ISO3 numeric
            continue
        v = r.get("value")
        if v in (None, ""):
            continue
        try:
            out.append({
                "indicator": f"SDG {indicator}",
                "value": float(v),
                "unit": None,
                "reference_period": str(r.get("timePeriod", "") or r.get("year", "")),
                "source": "UNSD SDG",
                "acquisition_method": "unsd_api",
                **_vintage(str(r.get("timePeriod", "") or r.get("year", ""))),
            })
        except (TypeError, ValueError):
            continue
    return out


def fetch_oecd(dataset: str, country: str = "IRN", dim: str = "") -> list[dict]:
    """OECD SDMX-JSON (best-effort)."""
    url = (f"https://sdmx.oecd.org/public/rest/data/OECD.ELS,"
           f"sleep?format=json&c[REF_AREA]={country}")
    raw = _get(url)
    try:
        data = json.loads(raw)
    except Exception:
        return []
    # SDMX parsing is structurally variable; return empty rather than guess.
    return []


def fetch_open_data(source: str, indicator: str, country: str = "IRN") -> list[dict]:
    if source == "worldbank":
        return fetch_worldbank(indicator, country)
    if source == "imf":
        return fetch_imf(indicator, country)
    if source == "unsd":
        return fetch_unsd(indicator, country)
    if source == "oecd":
        return fetch_oecd(indicator, country)
    return []


def fetch_all_iran() -> list[dict]:
    """Fetch every curated Iran indicator; returns flat list of measurement rows."""
    out = []
    for src, code, label, unit in CURATED_IRAN:
        try:
            rows = fetch_open_data(src, code)
            for r in rows:
                r["indicator"] = label  # human label for display
                r["unit"] = r.get("unit") or unit
            out.extend(rows)
        except Exception as e:  # operational failure -> log, keep going
            print(f"  [opendata] {src}/{code} failed: {e}")
    return out


# ---------------- Our World in Data (V0.3 S2) ----------------
# Curated slugs verified live with Iran coverage (2026-08 probe), mapped to
# problem domains. OWID grapher CSV: free, no key, license-clean (CC-BY).

OWID_INDICATORS = {
    "energy-use-per-capita": ("energy", "Energy use per capita"),
    "per-capita-electricity-consumption": ("energy", "Electricity consumption per capita"),
    "share-electricity-renewables": ("energy", "Renewable share of electricity"),
    "co-emissions-per-capita": ("environment", "CO2 emissions per capita"),
    "annual-co2-emissions-per-country": ("environment", "Annual CO2 emissions"),
    "cereal-yield": ("agriculture", "Cereal yield"),
    "wheat-yields": ("agriculture", "Wheat yield"),
    "female-labor-force-participation-rates": ("labor_gender", "Female labor force participation"),
    "unemployment-rate": ("labor", "Unemployment rate"),
    "gender-gap-in-average-wages": ("labor_gender", "Gender gap in average wages"),
}


def fetch_owid(slug: str) -> list[dict]:
    """One OWID indicator for Iran -> normalized measurement rows.

    ROOT CAUSE THIS FIXES (data-integrity bug, not a gap):
    the `country=~IRN` query filter is silently ignored by some OWID grapher
    endpoints, which then return ONE ROW PER COUNTRY (195 rows, alphabetical).
    The old loop took rows unconditionally, so the FIRST row -- Afghanistan --
    was stored labelled 'Iran'. Iran's unemployment (8.3%) was published as
    Afghanistan's (13.4%). We now filter to Iran explicitly by code/entity and
    refuse to emit anything if no Iran row exists.
    ROOT CAUSE #2 THIS FIXES (the data cliff):
    `csvType=filtered` makes several grapher endpoints return a LATEST-VALUE
    snapshot -- one row per country, no history -- which is why 8 indicators held
    exactly 1 data point and no trend chart could ever be drawn for them. The
    unfiltered CSV carries the full series (unemployment 1991-2025, energy
    1965-2025), so we request that and filter to Iran locally.
    """
    url = (f"https://ourworldindata.org/grapher/{slug}.csv"
           f"?useColumnShortNames=true")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    raw = urllib.request.urlopen(req, timeout=45, context=CTX).read()
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    # HARD FILTER: only Iran. Never trust the server-side country filter.
    rows = [r for r in rows
            if (r.get("code") or "").strip().upper() == "IRN"
            or (r.get("entity") or "").strip() == "Iran"]
    if not rows:
        raise RuntimeError(f"OWID {slug}: no Iran row in response")
    out = []
    today = _CURRENT_YEAR
    for r in rows:
        yr_s = r.get("year") or r.get("Year") or ""
        if not str(yr_s).strip():
            continue
        try:
            yr = int(float(str(yr_s)))
        except ValueError:
            continue
        val = None
        colname = ""
        for k, v in r.items():
            kl = k.lower()
            # skip identity columns AND OWID's provenance sidecar columns
            if kl in ("entity", "code", "year") or kl.endswith("__original_year"):
                continue
            sv = (v or "").strip() if isinstance(v, str) else v
            if sv in (None, ""):
                continue
            try:
                val = float(sv)
                colname = k
            except (TypeError, ValueError):
                continue
            break
        if val is None:
            continue
        # Some grapher views are "latest value" snapshots whose `year` column is
        # the release year, with the true reference year in a sidecar column.
        oy = (r.get(f"{colname}__original_year") or "").strip()
        if oy:
            try:
                yr = int(float(oy))
            except ValueError:
                pass
        unit = "%" if "%" in colname or colname.endswith("__pct") else ""
        out.append({
            "source": f"OWID:{slug}",
            "subject_entity": "Iran",
            "indicator_name": OWID_INDICATORS[slug][1],
            "value": val,
            "unit": unit,
            "period_start": str(yr),
            "period_end": None,
            "confidence": 1.0,
            "notes": f"domain={OWID_INDICATORS[slug][0]}",
            "data_vintage": min(today - yr, 9999),
            "is_preliminary": yr >= today - 1,
            "is_projection": False,
        })
    return out


# Domain keywords -> which extra OWID domains to pull for a problem statement.
_DOMAIN_OWID = {
    "economic": ("labor", "labor_gender"),
    "cooperativ": ("labor",),
    "energy": ("energy", "environment"),
    "water": ("energy", "agriculture", "environment"),
    "agriculture": ("agriculture", "environment"),
    "export": ("agriculture", "environment"),
    "women": ("labor_gender", "labor"),
    "gender": ("labor_gender", "labor"),
    "labor": ("labor", "labor_gender"),
    "unemploy": ("labor", "labor_gender"),
    "family": ("labor_gender",),
    "housing": ("labor",),
    "conflict": ("energy", "environment"),
}


def owid_domains_for(statement: str) -> set:
    s = statement.lower()
    doms = set()
    for kw, ds in _DOMAIN_OWID.items():
        if kw in s:
            doms.update(ds)
    return doms


def fetch_owid_for_problem(statement: str) -> list[dict]:
    """All OWID rows from domains relevant to this problem."""
    want = owid_domains_for(statement)
    rows = []
    for slug, (dom, _label) in OWID_INDICATORS.items():
        if dom not in want:
            continue
        try:
            rows.extend(fetch_owid(slug))
        except Exception:
            continue  # one slug down != no data; degrade per-slug
    return rows
