"""Investigation Layer — pipeline orchestration (ontology v1.10).

Given a Discovery-Layer Problem (id + fields), retrieve candidate studies
from OpenAlex (free), extract Findings + Interventions with the local LLM,
accumulate Interventions as first-class objects, and link them back to the
Problem with an Iran-relevance assessment. Isolated from Gate 2; joined to
the rest of the system only through shared objects (Problem, Entity,
Measurement, Intervention).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from ..database import Database
from .retrieval import build_query, fetch_studies, normalize_study
from .extraction import (
    extract_findings,
    extract_interventions,
    assess_iran_relevance,
    classify_relevance,
)
from .evidence_strategy import infer_evidence_strategy
from . import worldbank as wb_adapter


def _problem_dict(db: Database, problem_id: int) -> dict:
    row = db.conn.execute(
        """
        SELECT p.statement, p.polarity, p.artifact_id,
               t.name AS topic
        FROM problems p
        LEFT JOIN topics t ON p.topic_id = t.id
        WHERE p.id = ?
        """,
        (problem_id,),
    ).fetchone()
    if row is None:
        return {}
    questions = [
        {"id": r["id"], "question": r["question"], "rank": r["rank"]}
        for r in db.conn.execute(
            "SELECT id, question, rank FROM investigation_questions "
            "WHERE problem_id=? ORDER BY rank",
            (problem_id,),
        ).fetchall()
    ]
    entities = [
        r["name"]
        for r in db.conn.execute(
            """
            SELECT e.name FROM problem_entities pe
            JOIN entities e ON e.id = pe.entity_id
            WHERE pe.problem_id = ?
            """,
            (problem_id,),
        ).fetchall()
    ]
    return {
        "statement": row["statement"],
        "topic": row["topic"],
        "questions": questions,            # list of {id, question, rank}
        "question_texts": [q["question"] for q in questions],
        "entity_names": entities,
    }


def _extract_resilient(fn: Callable[[str], list], abstract: str, max_retries: int = 3) -> list:
    """Call an extraction fn; on a 429 rate-limit from the local model, back
    off briefly and retry; on other errors, return [] (degrade gracefully so
    one bad paper never aborts the whole Investigation run)."""
    for attempt in range(max_retries):
        try:
            return fn(abstract)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "429" in msg or "RateLimit" in msg:
                time.sleep(2.0 * (attempt + 1))
                continue
            return []
    return []


def _substrates_for(strategy: str) -> list:
    """Map an evidence-demand strategy to the substrates it requires."""
    return {
        "scholarly_study": ["scholarly"],
        "quantitative_official": ["quant"],
        "current_event": ["current"],
        "mixed": ["scholarly", "quant"],
    }.get(strategy, ["scholarly"])


def _question_status(substrate_outcomes: dict) -> str:
    """Decide a Question lifecycle status from its required-substrate outcomes.

    substrate_outcomes: {substrate: 'evidence'|'empty'|'operational'|'unmappable'}

    Epistemic rules (Step 11):
    - operational (API/rate-limit/source failure) MUST NOT become no_evidence
      -> 'deferred' (we could not conclude).
    - unmappable (no adapter / no indicator for a required substrate) is its
      own terminal state when it blocks a required substrate.
    - 'answered' requires at least one substrate produced evidence AND all
      attempted (no operational failure). Never "answered" just because some
      evidence was found while a required substrate failed.
    - 'no_evidence' = all attempted substrates returned nothing (genuine
      coverage/absence gap), distinct from failure.
    """
    vals = list(substrate_outcomes.values())
    if any(v == "operational" for v in vals):
        return "deferred"
    if any(v == "evidence" for v in vals):
        return "answered"
    if any(v == "unmappable" for v in vals):
        return "unmappable"
    return "no_evidence"


def investigate_problem(
    db: Database,
    problem_id: int,
    per_page: int = 5,
    only_oa: bool = True,
    use_relevance_gate: bool = True,
    frozen_studies: list | None = None,
) -> dict:
    """Run the Investigation Layer for one Problem. Returns a summary dict.

    Step 11 — Question-centered evidence lifecycle: each Investigation Question
    is routed by its own evidence-demand, its retrieved Findings/Measurements are
    linked back to it (many-to-many, rerun-safe), and its status is set ONLY
    after all required evidence substrates have been attempted. Operational /
    source failures are recorded as 'deferred', never as 'no_evidence'.
    """
    problem = _problem_dict(db, problem_id)
    if not problem:
        return {"problem_id": problem_id, "error": "problem not found"}

    query = build_query(problem)
    fetch_error = False
    if frozen_studies is not None:
        # Deterministic eval mode: use pre-frozen studies instead of hitting
        # OpenAlex (its top-5 varies per call -> non-reproducible audits).
        # Accepts BOTH shapes: corpus records (work_id + plain abstract) AND
        # already-normalized study dicts (from query_expansion multi-fetch).
        works = []
        for s in frozen_studies:
            if s.get("_frozen"):
                works.append(s)
            elif "abstract" in s and "source_work_id" in s:
                s = dict(s)
                s["_frozen"] = True
                works.append(s)
            else:
                w = _frozen_to_work(s)
                if not w["abstract"]:
                    # raw OpenAlex work: reconstruct the plain abstract via
                    # normalize_study instead of dropping it silently
                    n = normalize_study(s)
                    w["abstract"] = n["abstract"]
                    w["authors"] = n["authors"]
                    w["countries"] = n["countries"]
                    w["concepts"] = n["concepts"]
                    w["cited_by_count"] = n["cited_by_count"]
                    w["year"] = n["year"]
                    w["study_type"] = n["study_type"]
                    w["doi"] = n["doi"]
                works.append(w)
    else:
        try:
            works = fetch_studies(query, per_page=per_page, only_oa=only_oa)
        except Exception:  # noqa: BLE001  (operational OpenAlex failure)
            works = []
            fetch_error = True

    evidence_strategy = infer_evidence_strategy(problem, problem["question_texts"])
    summary = {
        "problem_id": problem_id,
        "query": query,
        "evidence_strategy": evidence_strategy["strategy"],
        "evidence_strategy_rationale": evidence_strategy["rationale"],
        "question_strategies": {},   # per-Question evidence-demand -> route
        "question_status": {},       # per-Question lifecycle status
        "measurements": 0,
        "studies_retrieved": 0,
        "studies_relevant": 0,
        "studies_deferred": 0,       # classifier ERROR (e.g. 429) -> unknown
        "studies": 0,
        "findings": 0,
        "interventions": 0,
        "intervention_ids": [],
        "question_evidence_links": 0,
    }

    questions = problem["questions"]  # list of {id, question, rank}
    # Per-Question evidence-demand, classified in ISOLATION (per_question=True)
    # so the parent Problem statement does not leak cross-cutting words
    # (e.g. "trade") into an individual Question's routing.
    q_strategies = {
        q["id"]: infer_evidence_strategy(problem, [q["question"]], per_question=True)["strategy"]
        for q in questions
    }
    # Per-Question substrate outcomes, filled as we attempt each substrate.
    # 'scholarly' is attempted once for the whole Problem (shared fetch) and
    # applies to every scholarly/mixed Question.
    q_outcomes = {q["id"]: {} for q in questions}

    # ---- Quantitative / current dispatch (World Bank), per Question --------
    # Quantitative/mixed Questions route to the official-data substrate
    # (Measurement); current_event has no adapter yet -> recorded unmappable.
    # One Measurement row may answer several Questions (linked to each).
    country = wb_adapter.select_country(problem.get("entity_names", []) or [])
    meas_by_key = {}  # (code, country, period) -> measurement id (insert once)
    for q in questions:
        qstrat = q_strategies[q["id"]]
        db.update_question_lifecycle(q["id"], qstrat, "pending")
        summary["question_strategies"][q["question"][:60]] = qstrat
        substrates = _substrates_for(qstrat)
        if "quant" in substrates and country:
            ind = wb_adapter.select_indicator(q["question"])
            if not ind:
                q_outcomes[q["id"]]["quant"] = "unmappable"
                continue
            try:
                res = wb_adapter.fetch_indicator_observations(
                    country, ind["code"], per_page=5)
            except Exception as e:  # noqa: BLE001  (operational WB failure)
                # Resilience parity with OpenAlex/scholarly: an external source
                # error is operational, NOT a crash. Record it and continue so
                # the rest of the run (and other Questions) still finalize.
                q_outcomes[q["id"]]["quant"] = "operational"
                summary["wb_operational_errors"] = (
                    summary.get("wb_operational_errors", 0) + 1)
                continue
            if res["status"] == "ok":
                for obs in res["observations"]:
                    key = (ind["code"], country, str(obs.get("date")))
                    if key not in meas_by_key:
                        # Step 55 fix (mis-attribution): World Bank series are
                        # COUNTRY-level. The subject must be the country, never
                        # the Problem's first entity (which may be a ministry,
                        # cooperative, or company). Attributing national
                        # indicators to an organization is factually wrong.
                        m = wb_adapter.normalize_observation(
                            country, ind, obs,
                            subject_entity_name=wb_adapter.country_display_name(country))
                        mid = db.get_or_create_measurement(
                            artifact_id=None,
                            indicator=m["indicator"],
                            subject_entity_id=None,
                            subject_entity_name=m["subject_entity_name"],
                            value=m["value"],
                            unit=m["unit"],
                            reference_period=m["reference_period"],
                            measurement_source=m["measurement_source"],
                            acquisition_method=m["acquisition_method"],
                            quality=m["quality"],
                        )
                        meas_by_key[key] = mid
                        summary["measurements"] += 1
                    # Link this Measurement to THIS Question (many-to-many).
                    db.link_question_evidence(q["id"], "measurement", meas_by_key[key],
                                             role="answers")
                    summary["question_evidence_links"] += 1
                q_outcomes[q["id"]]["quant"] = "evidence"
            elif res["status"] == "source_unavailable":
                q_outcomes[q["id"]]["quant"] = "operational"
            else:  # no_observation
                q_outcomes[q["id"]]["quant"] = "empty"
        if "current" in substrates:
            # No current-event adapter wired yet -> cannot be attempted.
            q_outcomes[q["id"]]["current"] = "unmappable"

    # ---- Scholarly dispatch (OpenAlex), once for the Problem -------------
    all_finding_ids = []
    deferred = []
    for work in works:
        study = work if work.get("_frozen") else normalize_study(work)
        if not study["title"]:
            continue
        summary["studies_retrieved"] += 1
        if use_relevance_gate:
            # Step 16: anchor the gate on the Investigation Question(s), not the
            # broad Problem statement. Findings are linked per-Question, so a
            # study must be judged against the Question need. When several
            # scholarly/mixed Questions exist, anchor on their combined text
            # (the study may answer any of them) plus light entity grounding.
            sch_q_texts = [q["question"] for q in questions
                           if q_strategies[q["id"]] in ("scholarly_study", "mixed")]
            anchor_q = " ".join(sch_q_texts) if sch_q_texts else None
            verdict = _extract_resilient(
                lambda ab: classify_relevance(
                    problem["statement"], study["title"], ab,
                    question_text=anchor_q,
                    entity_names=problem.get("entity_names", []) or []),
                study["abstract"],
            )
            rel = verdict.get("relevant") if isinstance(verdict, dict) else None
            if rel is True:
                summary["studies_relevant"] += 1
                all_finding_ids += _process_study(db, study, problem, problem_id, summary)
            elif rel is False:
                pass  # genuine semantic rejection -> discard
            else:
                deferred.append(study)  # classifier error -> defer, not reject
        else:
            summary["studies_relevant"] += 1
            all_finding_ids += _process_study(db, study, problem, problem_id, summary)

    # Step 5: lossless retry of deferred (errored) studies.
    for study in deferred:
        time.sleep(4.0)
        sch_q_texts = [q["question"] for q in questions
                       if q_strategies[q["id"]] in ("scholarly_study", "mixed")]
        anchor_q = " ".join(sch_q_texts) if sch_q_texts else None
        verdict = _extract_resilient(
            lambda ab: classify_relevance(
                problem["statement"], study["title"], ab,
                question_text=anchor_q,
                entity_names=problem.get("entity_names", []) or []),
            study["abstract"],
        )
        rel = verdict.get("relevant") if isinstance(verdict, dict) else None
        if rel is True:
            summary["studies_relevant"] += 1
            all_finding_ids += _process_study(db, study, problem, problem_id, summary)
        elif rel is False:
            pass
        else:
            summary["studies_deferred"] += 1

    # Scholarly substrate outcome for the Problem (applies to all scholarly/mixed Qs).
    if fetch_error:
        scholarly_outcome = "operational"
    elif summary["studies_deferred"] > 0 and summary["findings"] == 0:
        scholarly_outcome = "operational"  # all errored -> unknown, not rejected
    elif summary["findings"] > 0:
        scholarly_outcome = "evidence"
    else:
        scholarly_outcome = "empty"

    # Link findings to every scholarly/mixed Question (a study can answer many).
    scholarly_qs = [q["id"] for q in questions
                    if q_strategies[q["id"]] in ("scholarly_study", "mixed")]
    if scholarly_qs and all_finding_ids:
        for fid in all_finding_ids:
            for qid in scholarly_qs:
                db.link_question_evidence(qid, "finding", fid, role="answers")
                summary["question_evidence_links"] += 1

    # Set per-Question status once ALL required substrates are attempted.
    for q in questions:
        qstrat = q_strategies[q["id"]]
        substrates = _substrates_for(qstrat)
        outcomes = {}
        for s in substrates:
            if s == "scholarly":
                outcomes[s] = scholarly_outcome
            else:  # quant / current recorded per-Question above
                outcomes[s] = q_outcomes[q["id"]].get(s, "unmappable")
        status = _question_status(outcomes)
        # FSM-invariant guard: a Question must never remain permanently 'pending'
        # (e.g. if a future substrate branch throws before reaching here). A
        # stranded pending Question has no evidence and an unknown cause -> the
        # only honest final state is 'deferred' (could not conclude), never an
        # unrecoverable silent 'pending'.
        if status is None or status == "pending":
            status = "deferred"
        db.update_question_lifecycle(q["id"], qstrat, status)
        summary["question_status"][q["question"][:60]] = status
        # Minimal synthesis: a provenance-aware answer summary for answered
        # Questions. Deliberately NOT forecasting/LLM-generated prose -- just an
        # honest record of what evidence answered the Question. AnalysisRecord
        # deferred (Step 11: minimal answer field is sufficient for v0).
        if status == "answered":
            ans = synthesize_question_answer(db, q["id"])
            db.set_question_answer(q["id"], ans)
            summary["answers"] = summary.get("answers", {})
            summary["answers"][q["question"][:60]] = ans

    return summary


def _fmt_value(value: float, unit: str | None) -> str:
    """Deterministic, human-readable value formatting (no LLM, no inference)."""
    unit = (unit or "").strip()
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if unit:
        # Unit already carries the scale/dimension (e.g. "%", "tons"); show raw.
        return f"{v:,.2f} {unit}".rstrip()
    # No unit -> likely a raw magnitude (current US$ etc.); abbreviate for reading.
    if abs(v) >= 1e9:
        return f"{v/1e9:,.1f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:,.1f}M"
    if abs(v) >= 1e3:
        return f"{v:,.0f}"
    return f"{v:,.2f}"


def synthesize_question_answer(db: Database, question_id: int) -> str:
    """Build a minimal, deterministic, inspectable answer from linked evidence.

    Epistemic contract (Phase 14):
    - Derives ONLY from question_evidence + the referenced Measurement/Finding rows.
    - For quantitative evidence, exposes indicator / subject / source / per-period
      observations / count so a human understands what was learned WITHOUT joining
      the DB. No trend inference, no causal claim, no forecast.
    - Multiple indicators are kept as DISTINCT series (never mixed).
    - Mixed Questions separate Quantitative vs Scholarly evidence explicitly.
    - Never called for no_evidence / deferred / unmappable / pending.
    Rerun-safe: pure read; identical inputs -> identical answer.
    """
    meas_rows = db.conn.execute(
        "SELECT m.indicator, m.subject_entity_name, m.value, m.unit, "
        "m.reference_period, m.measurement_source FROM measurements m "
        "JOIN question_evidence qe ON qe.evidence_type='measurement' "
        "AND qe.evidence_id=m.id WHERE qe.question_id=? "
        "ORDER BY m.indicator, m.subject_entity_name, m.reference_period",
        (question_id,),
    ).fetchall()
    fcount = db.conn.execute(
        "SELECT COUNT(*) FROM question_evidence "
        "WHERE question_id=? AND evidence_type='finding'",
        (question_id,),
    ).fetchone()[0]
    f_sample = db.conn.execute(
        "SELECT f.statement FROM findings f "
        "JOIN question_evidence qe ON qe.evidence_type='finding' "
        "AND qe.evidence_id=f.id WHERE qe.question_id=? LIMIT 1",
        (question_id,),
    ).fetchone()

    quant_blocks = []
    if meas_rows:
        # Group observations into series by (indicator, subject, source).
        series: dict = {}
        for r in meas_rows:
            key = (r["indicator"], r["subject_entity_name"], r["measurement_source"])
            series.setdefault(key, []).append(r)
        for (indicator, subject, source), obs in series.items():
            obs_sorted = sorted(obs, key=lambda x: str(x["reference_period"]))
            pts = "; ".join(
                f"{o['reference_period']}: {_fmt_value(o['value'], o['unit'])}"
                for o in obs_sorted
            )
            who = f"{subject} | " if subject else ""
            quant_blocks.append(
                f"{source} — {who}{indicator}: {pts} ({len(obs_sorted)} obs)"
            )

    scholarly = ""
    if fcount:
        scholarly = f"{fcount} scholarly finding(s)"
        if f_sample and f_sample["statement"]:
            scholarly += f". Sample: " + '"' + f_sample['statement'][:160] + '"'

    if quant_blocks and scholarly:
        # Mixed Question: keep substrates visibly distinct.
        return ("Quantitative:\n- " + "\n- ".join(quant_blocks) +
                "\nScholarly: " + scholarly)
    if quant_blocks:
        return "\n".join(quant_blocks)
    if scholarly:
        return scholarly
    return "Answered (evidence linked)."


def _frozen_to_work(rec: dict) -> dict:
    """Convert a frozen-corpus study record (eval/fixtures/corpus_v1.json) into a
    normalized study dict usable directly by _process_study (bypasses
    normalize_study, which would drop the plain abstract string). Flagged _frozen
    so the Scholarly dispatch loop uses it as-is."""
    return {
        "_frozen": True,
        "source_work_id": rec.get("work_id"),
        "title": rec.get("title", ""),
        "year": rec.get("year"),
        "study_type": None,
        "authors": [],
        "institutions": [],
        "countries": [],
        "doi": None,
        "landing_url": rec.get("url"),
        "pdf_url": None,
        "abstract": rec.get("abstract") or "",
        "concepts": [],
        "cited_by_count": 0,
    }


def _process_study(db, study, problem, problem_id, summary) -> list:
    """Insert a study and extract its Findings + Interventions. Returns the
    list of finding ids created (so the pipeline can link them to Questions)."""
    study_id = db.get_or_create_study(
        title=study["title"],
        source_work_id=study["source_work_id"],
        year=study["year"],
        study_type=study["study_type"],
        authors=study["authors"],
        institutions=study["institutions"],
        countries=study["countries"],
        doi=study["doi"],
        landing_url=study["landing_url"],
        pdf_url=study["pdf_url"],
        abstract=study["abstract"],
        concepts=study["concepts"],
        cited_by_count=study["cited_by_count"],
    )
    summary["studies"] += 1
    finding_ids = []

    _found = _extract_resilient(extract_findings, study["abstract"]) or []
    if not _found:
        # Fallback: the strict intervention-only prompt returns [] for data
        # studies with real quantitative results (live: sanctions-on-Iran
        # synthetic-control paper with 19.1%-of-GDP effect got zero). The
        # analysis-results rule recovers them as correlational findings.
        try:
            from .extraction import _parse_json_block, _chat, _FINDING_SYS
            _rule = ("ADDITIONAL RULE: analysis results of DATA STUDIES "
                     "count as findings even without an intervention: if the "
                     "study quantifies a relationship, trend, or share, "
                     "extract it as a finding with causal_strength="
                     "'correlational' and empty intervention_name/relation.")
            _raw = _chat(_FINDING_SYS + _rule,
                         f"ABSTRACT:\n{(study['abstract'] or '')[:3500]}")
            _found = (_parse_json_block(_raw).get("findings") or [])
        except Exception:
            _found = []
    for f in _found:
        fid = db.get_or_create_finding(
            study_id=study_id,
            statement=f.get("statement", ""),
            population=f.get("population"),
            context=f.get("context"),
            intervention_name=f.get("intervention_name"),
            outcome=f.get("outcome"),
            effect_size=f.get("effect_size"),
            causal_strength=f.get("causal_strength"),
            geographic_applicability=f.get("geographic_applicability"),
            limitations=f.get("limitations"),
        )
        finding_ids.append(fid)
        summary["findings"] += 1
        iname = (f.get("intervention_name") or "").strip()
        irelation = (f.get("relation") or "").strip()
        if iname and irelation in ("supports", "evaluates", "reports_failure"):
            iid = db.upsert_intervention(name=iname, target_problem_domain=problem.get("topic"))
            db.link_finding_intervention(fid, iid, relation=irelation)
            if iid not in summary["intervention_ids"]:
                summary["intervention_ids"].append(iid)
            summary["interventions"] += 1

    for iv in _extract_resilient(extract_interventions, study["abstract"]):
        name = (iv.get("name") or "").strip()
        if not name:
            continue
        iid = db.upsert_intervention(
            name=name,
            description=iv.get("description"),
            type_=iv.get("type"),
            target_problem_domain=iv.get("target_problem_domain") or problem.get("topic"),
            source_provenance=study["source_work_id"],
        )
        ctx = f"{study.get('countries')} | {iv.get('target_problem')}"
        rel = _extract_resilient(assess_iran_relevance, name + "\n" + ctx) or {}
        if not rel:
            rel = {}
        db.link_problem_intervention(
            problem_id, iid,
            relevance_to_iran=rel.get("relevance_to_iran"),
            adoption_barriers=rel.get("adoption_barriers") or iv.get("adoption_barriers"),
        )
        if iid not in summary["intervention_ids"]:
            summary["intervention_ids"].append(iid)
        summary["interventions"] += 1

    return finding_ids
