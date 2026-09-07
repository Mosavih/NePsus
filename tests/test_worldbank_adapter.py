"""Tests for Step 10 World Bank adapter + per-Question routing.

Verifies the boundary: WB observations -> Measurement, never Study/Finding;
distinct failure modes; deterministic indicator/country selection.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(".").resolve()))

from src.investigation_layer import worldbank as wb


def test_select_indicator():
    assert wb.select_indicator("What is the scale of bilateral trade?")["code"] == "NE.EXP.GNFS.CD"
    assert wb.select_indicator("GDP growth matters")["code"] == "NY.GDP.MKTP.CD"
    assert wb.select_indicator("long-term security impacts") is None  # unmappable


def test_select_country():
    assert wb.select_country(["Iran", "Afghanistan"]) == "IR"
    assert wb.select_country(["Pakistan"]) == "PK"
    assert wb.select_country(["United Nations"]) is None


def test_normalize_observation():
    ind = {"code": "NE.EXP.GNFS.CD", "name": "Exports of goods and services (current US$)"}
    obs = {"date": "2023", "value": 109444045103.672, "obs_status": "", "indicator": {}}
    m = wb.normalize_observation("IR", ind, obs, "Iran")
    assert m["indicator"] == ind["name"]
    assert m["value"] == 109444045103.672
    assert m["reference_period"] == "2023"
    assert m["measurement_source"] == "World Bank WDI"
    assert m["acquisition_method"] == "worldbank_api"
    assert m["subject_entity_name"] == "Iran"


def test_fetch_real_iran_exports():
    # Real API call (rate-limit/network may fail -> explicit unavailable, not fake).
    res = wb.fetch_indicator_observations("IR", "NE.EXP.GNFS.CD", per_page=3)
    assert res["status"] in ("ok", "no_observation", "source_unavailable")
    if res["status"] == "ok":
        assert len(res["observations"]) >= 1
        # skip-missing-value behaviour: every returned obs has a value
        assert all(o.get("value") is not None for o in res["observations"])
