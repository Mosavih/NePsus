"""T2 -- Evidence dossier builder: rich structured input for the composer.

Assembles everything Investigation knows about a Problem into a single
structured dossier that a narrative composer can reason over WITHOUT inventing:

  problem: id, statement, iran verdict + note
  measurements: series per indicator (id, period, value, unit) + deterministic
      patterns (direction, % change) -- computed here, cited by id range
  findings: id, statement, study/year/citations, geo applicability, causal
      strength, intervention name, outcome
  interventions: verified candidate levers (+ unverified flagged)
  gaps: explicit unresolved reasons per question
  evidence_coverage: counts, iran-specific vs comparator

Every item carries its DB id so the fabrication guard can check each number/claim
in the composed post against this dossier.

Deterministic. No LLM calls.
"""
from __future__ import annotations


def _series_patterns(rows):
    """rows: [(period, value)] sorted by period -> patterns dict."""
    vals = [v for _, v in rows if v is not None]
    pats = {}
    if len(vals) >= 2:
        first, last = vals[0], vals[-1]
        pats["first_period"] = rows[0][0]
        pats["last_period"] = rows[-1][0]
        pats["first_value"] = first
        pats["last_value"] = last
        if first:
            pats["pct_change"] = round((last - first) / abs(first) * 100, 1)
        diffs = [b - a for a, b in zip(vals, vals[1:])]
        if all(d >= 0 for d in diffs):
            pats["direction"] = "increasing"
        elif all(d <= 0 for d in diffs):
            pats["direction"] = "decreasing"
        else:
            pats["direction"] = "mixed"
    return pats


from datetime import date as _date

# Temporal grounding (V0.1 fix): the pipeline had NO awareness of today's date,
# which let the composer cherry-pick baselines (2013 inflation spike) and present
# stale or projected data as current. These helpers inject honest time context.


def _temporal_summary(series, today=None):
    """Mechanical summary of one measurement series with vintage honesty.

    Rate-type series (inflation %, growth %, shares) get endpoint framing instead
    of percent-change-of-a-percent, which is meaningless.
    """
    today = today or _date.today()
    this_yr = today.year
    pts = series.get("points", [])
    pats = series.get("patterns", {})
    if not pts:
        return ""
    name = str(series.get("indicator", "")).lower()
    _unit = str(series.get("unit") or "")
    rate_like = ("%" in name or "rate" in name or "growth" in name
                 or "inflation" in name or "share" in name or "%" in _unit)
    years = []
    for p in pts:
        try:
            years.append(int(str(p["period"])[:4]))
        except (TypeError, ValueError):
            continue
    # MONTHLY PRECISION (Phase A, 2026-09-03): a 'YYYY-MM' point in the current
    # year is an observation, not a forecast -- only bare-year current-year
    # points count as projections.
    def _is_proj(p) -> bool:
        try:
            y = int(str(p["period"])[:4])
        except (TypeError, ValueError):
            return False
        if y > this_yr:
            return True
        return y == this_yr and len(str(p["period"])) == 4
    n_proj = sum(1 for p in pts if _is_proj(p))
    latest_actual = max((int(str(p["period"])[:4]) for p in pts
                         if not _is_proj(p)), default=None)
    parts = [f"window {pats.get('first_period')}-{pats.get('last_period')}"]
    d = pats.get("direction")
    if d:
        parts.append(f"{d} overall")
    fv, lv = pats.get("first_value"), pats.get("last_value")
    fp_, lp_ = pats.get("first_period"), pats.get("last_period")
    if rate_like:
        if fv is not None and lv is not None:
            parts.append(f"moved from {fv:,.1f} ({fp_}) to {lv:,.1f} ({lp_}) -- "
                         f"a change of the RATE itself, not of prices/output")
    elif pats.get("pct_change") is not None:
        parts.append(f"net change {pats['pct_change']:+.1f}% over the full window")
    if lv is not None:
        parts.append(f"latest value {lv:,.1f} ({lp_})")
    if latest_actual:
        parts.append(f"latest ACTUAL year with observed data: {latest_actual}")
    else:
        parts.append("no observed (non-projection) data in this series")
    if n_proj:
        parts.append(f"WARNING: {n_proj} point(s) at/after {this_yr} are PROJECTIONS,"
                     f" not observations -- label them as forecasts or omit them")
    age = this_yr - (latest_actual or this_yr)
    if age >= 2:
        parts.append(f"STALENESS: newest observation is ~{age} years old; "
                     f"say so instead of implying it is current")
    elif latest_actual and latest_actual >= this_yr - 1:
        parts.append("NOTE: values for the most recent year(s) are often "
                     "PRELIMINARY estimates by the source; call them initial "
                     "estimates rather than final figures")
    return "; ".join(parts)

def build_dossier(db, problem_id: int) -> dict:
    conn = db._require_connection()
    prow = conn.execute(
        "SELECT id, statement, iran_relevant, iran_note FROM problems WHERE id=?",
        (problem_id,)).fetchone()
    if not prow:
        raise ValueError(f"problem {problem_id} not found")
    # scenario column is newer than some DBs: read defensively.
    try:
        srow = conn.execute("SELECT scenario FROM problems WHERE id=?",
                            (problem_id,)).fetchone()
        scenario = (srow["scenario"] if srow and srow["scenario"] else "")
    except Exception:
        scenario = ""

    dossier = {
        "problem_id": problem_id,
        "statement": prow["statement"],
        "iran_relevant": prow["iran_relevant"],
        "iran_note": prow["iran_note"],
        "questions": [],
        "measurements": [],   # flattened series entries with ids
        "measurement_series": {},  # indicator -> {subject, points, patterns}
        "claims": [],         # source claims (news provenance) via question_evidence
        "findings": [],
        "interventions": {"verified": [], "unverified": []},
        "gaps": [],
        "news_context": "",
        "scenario": scenario,
        "coverage": {},
    }

    qrows = conn.execute(
        "SELECT id, question, status FROM investigation_questions "
        "WHERE problem_id=? ORDER BY rank", (problem_id,)).fetchall()

    seen_meas, seen_find = set(), set()
    sig_tokens = []          # token-sets of kept findings (Jaccard dedupe)
    n_iran_geo = 0
    for q in qrows:
        qd = {"question_id": q["id"], "question": q["question"], "status": q["status"]}
        linked = conn.execute(
            "SELECT evidence_type, evidence_id FROM question_evidence "
            "WHERE question_id=?", (q["id"],)).fetchall()
        ev_ids = []
        for e in linked:
            if e["evidence_type"] == "measurement":
                mid = e["evidence_id"]
                m = conn.execute(
                    "SELECT id, indicator, subject_entity_name, value, unit, "
                    "reference_period, measurement_source, quality, status, provisional "
                    "FROM measurements WHERE id=?", (mid,)).fetchone()
                if m and mid not in seen_meas:
                    seen_meas.add(mid)
                    # P0 honesty: projections (IMF WEO forecast years) are not
                    # observations -- they never enter the composer's evidence.
                    if m["status"] == "projection":
                        ev_ids.append(f"{e['evidence_type']}:{e['evidence_id']}")
                        continue
                    dossier["measurements"].append({
                        "id": m["id"], "indicator": m["indicator"],
                        "subject": m["subject_entity_name"],
                        "value": m["value"], "unit": m["unit"],
                        "period": str(m["reference_period"]),
                        "source": m["measurement_source"],
                        "provisional": bool(m["provisional"]),
                    })
            elif e["evidence_type"] == "claim":
                cid = e["evidence_id"]
                cl = conn.execute(
                    "SELECT id, proposition, claim_type, temporal_scope, time, "
                    "artifact_id FROM claims WHERE id=?", (cid,)).fetchone()
                if cl and cid not in seen_find:
                    seen_find.add(cid)
                    arow = conn.execute(
                        "SELECT original_url FROM source_artifacts WHERE id=?",
                        (cl["artifact_id"],)).fetchone()
                    dossier["claims"].append({
                        "id": cl["id"],
                        "proposition": cl["proposition"],
                        "claim_type": cl["claim_type"],
                        "temporal_scope": cl["temporal_scope"],
                        "when": cl["time"],
                        "url": arow["original_url"] if arow else None,
                    })
            elif e["evidence_type"] == "finding":
                fid = e["evidence_id"]
                f = conn.execute(
                    "SELECT id, statement, population, outcome, intervention_name, "
                    "geographic_applicability, causal_strength, study_id "
                    "FROM findings WHERE id=?", (fid,)).fetchone()
                if f and fid not in seen_find:
                    # Findings repeat across pipeline runs (same study processed
                    # multiple times, sometimes with trivial rewording: MRL vs
                    # "maximum residue limit (MRL)", ' vs '). Dedupe on
                    # Jaccard token similarity >= 0.72 against kept statements.
                    import re as _re
                    toks = set(_re.findall(r"[a-z0-9]+", (f["statement"] or "").lower()))
                    dup = False
                    for kept in sig_tokens:
                        inter = len(toks & kept)
                        if not inter:
                            continue
                        jac = inter / len(toks | kept)
                        if jac >= 0.72:
                            dup = True
                            break
                    if dup:
                        seen_find.add(fid)
                        ev_ids.append(f"{e['evidence_type']}:{e['evidence_id']}")
                        continue
                    sig_tokens.append(toks)
                    seen_find.add(fid)
                    geo = f["geographic_applicability"]
                    if geo and "iran" in (geo or "").lower():
                        n_iran_geo += 1
                    srow = None
                    if f["study_id"]:
                        srow = conn.execute(
                            "SELECT title, year, cited_by_count "
                            "FROM studies WHERE id=?", (f["study_id"],)).fetchone()
                    dossier["findings"].append({
                        "id": f["id"], "statement": f["statement"],
                        "population": f["population"], "outcome": f["outcome"],
                        "intervention": f["intervention_name"],
                        "geo": geo, "causal_strength": f["causal_strength"],
                        "study_id": f["study_id"],
                        "study_year": (srow["year"] if srow else None),
                        "cited_by": srow["cited_by_count"] if srow else None,
                    })
            ev_ids.append(f"{e['evidence_type']}:{e['evidence_id']}")
        qd["evidence_ids"] = ev_ids
        if q["status"] != "answered":
            reason = {
                "no_evidence": "no relevant evidence was retrieved",
                "deferred": "retrieval/source failed during investigation (recoverable)",
                "unmappable": "no adapter for this question's substrate yet",
                "pending": "not investigated yet",
            }.get(q["status"], q["status"])
            dossier["gaps"].append({"question_id": q["id"], "status": q["status"],
                                    "reason": reason})
        dossier["questions"].append(qd)

    # SCENARIO BACKFILL (2026-09-05 r18): spec-named numbers must be EVIDENCE.
    # r18 root cause: the 56.4 keystone lived only in the scenario spec text
    # while the dossier carried no fuel-share series -- the composer was
    # ordered to cite a number it was never given a source for, so it kept
    # reaching for sourced substitutes instead. For scenario problems, every
    # non-year spec number is resolved to Iran measurements BY VALUE and the
    # full indicator series is pulled in, so attribution flows normally.
    try:
        _spec = dossier.get("scenario") or ""
        if _spec:
            import re as _re2
            from src.investigation_layer.fa_norm import _num_norm as _nn2
            _have_inds = {m["indicator"]
                          for m in dossier["measurements"]}
            for _n in _re2.findall(r"\d+(?:\.\d+)?", _nn2(_spec)):
                # Decimals only (r18: integer "70" value-collided with urban
                # population; integers are trigger levels, already present).
                if "." not in _n:
                    continue
                _rows = conn.execute(
                    "SELECT id, indicator, subject_entity_name, value, unit, "
                    "reference_period, measurement_source, provisional "
                    "FROM measurements WHERE ABS(value - ?) < 0.051 "
                    "AND (status IS NULL OR status != 'projection') "
                    "LIMIT 5", (float(_n),)).fetchall()
                for _r in _rows:
                    if _r["indicator"] in _have_inds:
                        continue
                    _have_inds.add(_r["indicator"])
                    for _s in conn.execute(
                            "SELECT id, indicator, subject_entity_name, "
                            "value, unit, reference_period, "
                            "measurement_source, provisional "
                            "FROM measurements WHERE indicator=? "
                            "AND (status IS NULL OR status != 'projection') "
                            "ORDER BY reference_period",
                            (_r["indicator"],)).fetchall():
                        if _s["id"] in seen_meas:
                            continue
                        seen_meas.add(_s["id"])
                        dossier["measurements"].append({
                            "id": _s["id"],
                            "indicator": _s["indicator"],
                            "subject": _s["subject_entity_name"],
                            "value": _s["value"], "unit": _s["unit"],
                            "period": str(_s["reference_period"]),
                            "source": _s["measurement_source"],
                            "provisional": bool(_s["provisional"]),
                        })
    except Exception:
        pass

    # Group measurement series by (indicator, subject) + compute patterns.
    groups = {}
    units = {}
    srcs = {}  # (indicator, subject) -> first non-empty source (r14 fix)
    prov_years = {}     # (indicator, subject) -> set of provisional years
    for m in dossier["measurements"]:
        key = (m["indicator"], m["subject"])
        groups.setdefault(key, []).append((m["period"], m["value"]))
        if m.get("unit"):
            units[key] = m["unit"]
        if m.get("source"):
            srcs.setdefault(key, m["source"])
        if m.get("provisional"):
            prov_years.setdefault(key, set()).add(m["period"])
    for (ind, subj), pts in groups.items():
        pts.sort(key=lambda x: x[0])
        ids = [m["id"] for m in dossier["measurements"]
               if m["indicator"] == ind and m["subject"] == subj]
        _unit = units.get((ind, subj), "")
        # FA-NAME (live P27, 2026-09-03): the composer echoes whatever name the
        # dossier shows, so English-only headers leak Latin into the Persian
        # body ("Battle-related deaths", "GDP"). Show the registry's Persian
        # label + one-line gloss as the authoritative name; keep English only
        # as a parenthetical for claim_check id-mapping.
        _fa, _gl = "", ""
        try:
            _rr = conn.execute("SELECT label_fa, gloss_fa FROM metric_registry WHERE indicator=?",
                               (ind,)).fetchone()
            if _rr:
                _fa, _gl = (_rr[0] or "").strip(), (_rr[1] or "").strip()
        except Exception:
            pass
        _label = f"{ind} | {subj}" + (f" [{_unit}]" if _unit else "")
        # r26: the " — gloss" tail rode in the evidence name line and the
        # composer copied it verbatim into prose ("(دلار/بشکه) — قیمت جهانی
        # ..."). Bare name only; definitions live in the glossary feature.
        _fa_line = f"نام فارسی: {_fa}" if _fa else ""
        _pys = prov_years.get((ind, subj), set())
        dossier["measurement_series"][_label] = {
            "indicator": ind,
            "fa_name": _fa_line,
            "ids": ids,
            "unit": _unit,
            "source": srcs.get((ind, subj), ""),
            "points": [{"period": p, "value": v} for p, v in pts],
            "patterns": _series_patterns(pts),
            # P0 honesty: surfaced to the renderer, which tells the writer the
            # latest point is a provider estimate, not a published statistic.
            "provisional_last_year": (pts[-1][0] if pts and pts[-1][0] in _pys
                                      else None),
        }
    # Interventions (verified vs unverified).
    irows = conn.execute(
        """SELECT i.name, i.type, pi.relevance_to_iran, pi.adoption_barriers
           FROM problem_interventions pi
           JOIN interventions i ON i.id=pi.intervention_id
           WHERE pi.problem_id=?
             AND (i.status IS NULL OR i.status != 'quarantined')""",
        (problem_id,)).fetchall()
    for r in irows:
        rec = {"name": r["name"], "type": r["type"],
               "relevance_to_iran": r["relevance_to_iran"],
               "adoption_barriers": r["adoption_barriers"]}
        if (r["relevance_to_iran"] or "").strip():
            dossier["interventions"]["verified"].append(rec)
        else:
            dossier["interventions"]["unverified"].append(rec)

    # Live news context (V0.1): what is happening NOW around this problem.
    # Framing only -- never statistical evidence.
    try:
        from src.investigation_layer.news import context_for
        dossier["news_context"] = context_for(
            prow["statement"], prow["iran_note"] or "")
    except Exception as _ne:
        dossier["news_context"] = ""  # feeds down != compose blocked

    # Coverage summary.
    n_meas = len(dossier["measurements"])
    n_find = len(dossier["findings"])
    dossier["coverage"] = {
        "questions": len(qrows),
        "answered": sum(1 for q in qrows if q["status"] == "answered"),
        "measurements": n_meas,
        "findings": n_find,
        "iran_specific_findings": n_iran_geo,
        "comparator_only": bool(n_find and not n_iran_geo and not n_meas),
        "n_claims": len(dossier["claims"]),
        "has_evidence": bool(n_meas or n_find or dossier["claims"]),
    }
    return dossier


def format_dossier_for_composer(d: dict) -> str:
    """Render the dossier as compact text for an LLM composer prompt."""
    from datetime import date as _d2
    today = _d2.today()
    L = []
    L.append(f"TODAY: {today.isoformat()} — همه اعداد باید نسبت به این تاریخ "
             f"صادقانه قاب‌بندی شوند (سال جاری {today.year}؛ آخرین سال کامل مشاهده‌شده "
             f"{today.year - 1}).")
    L.append(f"PROBLEM (id={d['problem_id']}): {d['statement']}")
    L.append(f"IRAN SCOPE: {d['iran_relevant']} - {d['iran_note']}")
    L.append("")
    if d.get("scenario"):
        # LANE B (scenario posts, 2026-09-05): conditional arithmetic on
        # backward-looking numbers. The trigger is hypothetical; every number
        # cited must still come from the evidence series below. No point
        # prediction, no presented-as-fact future.
        L.append("SCENARIO (فرضیهٔ شرطی — نه پیش‌بینی): " + d["scenario"])
        L.append("قواعد سناریو: ۱) ماشه (سطح فرضی) را در همان پاراگراف اول با "
                 "کلمهٔ «اگر» صریح کن. ۲) زنجیره را قدم‌به‌قدم با اعداد شواهد "
                 "ببند (سهم × سطح = اثر)؛ هر حلقه باید نام سنجه و سالش را "
                 "داشته باشد؛ اثر را بازه‌ای بگو و از قطعیت مکانیکی («کاملاً "
                 "خطی») پرهیز کن. ۳) پایان: بگو تحت این فرض، هزینه اول بر دوش "
                 "کیست. ۴) هیچ عدد آینده‌ای نساز؛ باند عدم‌قطعیت را با بازه "
                 "بگو («بین X و Y») نه نقطه. ۵) واژهٔ «پیش‌بینی» در پست سناریویی "
                 "ممنوع — همیشه «فرض/سناریو/اگر». ۶) ماشهٔ ماهانهٔ تازه (مثل "
                 "برنت سپتامبر ۲۰۲۶) خودش لنگر «اکنون» است؛ هر جمله‌ای که "
                 "نکته‌اش فقط قدمت سری‌های سالانه باشد (با هر لفظی: کهنگی، "
                 "دو سال پیش، فاصله زمانی، تازه‌ترین/آخرین مشاهده) حذف شود."
                 " ۷) پریمیوم جنگی را «صرف "
                 "جنگی» بگو؛ واژهٔ «پاداش» (به‌معنای جایزه) برای قیمت نفت "
                 "غلط است. ۸) پاراگراف پایانی فقط می‌گوید «تحت این فرض، هزینه "
                 "بر دوش کیست»؛ هر جملهٔ پیش‌بینانهٔ تازه در پایان (پیش‌بینی "
                 "کوتاه‌مدت، انتظار می‌رود که...) ممنوع.")
    for key, ser in d["measurement_series"].items():
        ids = ",".join(str(i) for i in ser["ids"])
        L.append(f"[MEASUREMENTS {ids}] {key}")
        # FA-NAME: Persian preferred, but English names are fine untranslated
        # (2026-09-05 user: some names simply have no translation; the
        # two-line glossary format already prevents script mixing). Never
        # COERCE ("only this name") -- coercion caused the "X (X)" stutter.
        if ser.get("fa_name"):
            L.append(f"  {ser['fa_name']}")
        # P3: full precision, not :,.0f -- a post legitimately citing "8.3"
        # must find 8.3 in the evidence; integer-rounded evidence made exact
        # numeric grounding impossible and broke claim-level checks.
        pts = ", ".join(f"{p['period']}:{p['value']:g}" for p in ser["points"])
        L.append(f"  series: {pts}")
        # ATTRIBUTION ROOT FIX (2026-09-05): the composer kept failing the
        # source-beside-number rule because the source was never IN the
        # evidence text. It is now, on every series.
        # (r14: series points carry no source; read ser["source"].)
        _srcs = [ser.get("source") or ""]
        _srcs = [s for s in _srcs if s]
        if _srcs:
            # SOURCE_FA (2026-09-05 r7): provider names must be Persian --
            # English names ("World Bank WDI") collide with the FA-NAME-only
            # rule, so the model weaseled ("طبق آمارهای رسمی") or stuttered
            # ("قیمت نفت برنت (نفت برنت)"). Map once, here.
            from src.investigation_layer.fa_norm import source_fa as _fa_src
            _fa = "، ".join(dict.fromkeys(_fa_src(s) for s in _srcs))
            L.append(f"  منبع: {_fa} — همین نام فارسی را کنار اولین عدد این "
                     f"سری در متن بیاور (یک بار، بدون پرانتز تکراری نام سنجه).")
        ts = _temporal_summary(ser)
        if ts:
            L.append(f"  TEMPORAL FACTS (authoritative -- do not contradict): {ts}")
        # P0 honesty: the newest point of many provider series is a modelled
        # fill, not a published statistic. Say so IN the evidence so the writer
        # never frames a provisional value as an observed fact.
        if ser.get("provisional_last_year"):
            L.append(f"  NOTE: آخرین نقطه این سری ({ser['provisional_last_year']}) "
                     f"برآورد اولیهٔ ارائه‌دهنده است، نه آمار رسمی منتشرشده؛ "
                     f"در متن «برآورد اولیه» را ذکر کن یا از سال کامل قبلی استفاده کن.")
        L.append("")
    if d.get("claims"):
        L.append("")
        L.append("SOURCE CLAIMS (from news reporting -- provenance given; these are")
        L.append("journalistic assertions, NOT authoritative statistics; use only with")
        L.append("attribution like 'بر پایه گزارش‌ها'):")
        for cl in d["claims"][:14]:
            when = f", {cl['when']}" if cl["when"] else ""
            scope = cl["temporal_scope"] or "?"
            url = cl["url"] or "n/a"
            # P3 binding: emit the claim id so claim_check can map post numbers
            # to journalistic-evidence ids, not just measurement ids
            L.append(f"[CLAIM {cl['id']}] [{scope}{when}] {cl['proposition']}")
            L.append(f"  (source: {url})")

    for f in d["findings"]:
        meta = []
        if f["geo"]:
            meta.append(f"geo={f['geo']}")
        if f["causal_strength"]:
            meta.append(f"strength={f['causal_strength']}")
        if f["study_year"]:
            meta.append(f"year={f['study_year']}")
        if f["cited_by"]:
            meta.append(f"cited_by={f['cited_by']}")
        L.append(f"[FINDING {f['id']}] ({'; '.join(meta)}) {f['statement']}")
    if d["findings"]:
        L.append("")
    if d["interventions"]["verified"]:
        L.append("VERIFIED INTERVENTION LEVERS:")
        for i in d["interventions"]["verified"]:
            L.append(f"  - {i['name']} (Iran relevance: {i['relevance_to_iran']})")
        L.append("")
    if d["gaps"]:
        L.append("GAPS (be honest about these):")
        for g in d["gaps"]:
            L.append(f"  - Q{g['question_id']}: {g['reason']}")
        L.append("")
    if d.get("news_context"):
        L.append(d["news_context"])
        L.append("")
    c = d["coverage"]
    L.append(f"COVERAGE: {c['answered']}/{c['questions']} questions answered; "
             f"{c['measurements']} measurements, {c['findings']} findings "
             f"({c['iran_specific_findings']} Iran-specific)")
    if c["comparator_only"]:
        L.append("NOTE: all findings are international comparators, none Iran-specific.")
    return "\n".join(L)
