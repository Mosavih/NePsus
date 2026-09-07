"""V0.3 S3b -- intake: promote discovered candidate problems to composable.

A candidate becomes 'ready' when:
  - iran_relevant == 'yes'
  - has >= 2 investigation_questions (Gate 2 usually creates them; else
    scaffold from statement + artifact claims)
  - its questions carry evidence: quantitative/factual claims from the
    originating article + any OWID macro series for the topic domain

Usage:
  python eval/intake.py            # promote all eligible candidates
  python eval/intake.py --list     # show candidate status only
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv
if not load_dotenv(".env"):
    raise SystemExit("no .env")

import sqlite3

from src.database import Database
from src.investigation_layer.opendata import owid_domains_for, OWID_INDICATORS


def _claims_for_problem(conn: sqlite3.Connection, problem_id: int) -> list[sqlite3.Row]:
    """Claims from artifacts this problem cites as evidence."""
    return conn.execute(
        """
        SELECT DISTINCT c.id, c.proposition, c.claim_type, c.temporal_scope,
               c.time, c.artifact_id
        FROM claims c
        JOIN problem_evidence pe
          ON pe.evidence_type='artifact' AND pe.evidence_id=c.artifact_id
        WHERE pe.problem_id=?
        ORDER BY c.id
        """,
        (problem_id,),
    ).fetchall()


def _quantitative_score(proposition: str) -> int:
    import re
    digits = len(re.findall(r"\d", proposition))
    pct = len(re.findall(r"%|percent|میلیون|هزار|billion|million|thousand",
                         proposition.lower()))
    return digits + 2 * pct


def scaffold_questions_if_missing(db: Database, conn: sqlite3.Connection,
                                  pid: int) -> int:
    have = conn.execute(
        "SELECT COUNT(*) FROM investigation_questions WHERE problem_id=?",
        (pid,)).fetchone()[0]
    if have >= 2:
        return 0
    prow = conn.execute("SELECT statement FROM problems WHERE id=?", (pid,)).fetchone()
    claims = _claims_for_problem(conn, pid)
    # mechanical scaffolding: one question per distinct claim topic + one macro
    qs = []
    if claims:
        qs.append(f"کدام ادعاهای کمّی گزارش‌شده این مسئله را اندازه‌گیری می‌کنند؟")
    qs.append("این مسئله در داده‌های کلان موجود ایران چگونه دیده می‌شود؟")
    added = 0
    for rank, q in enumerate(qs[:2], start=1):
        db.insert_investigation_question(pid, q, rank)
        added += 1
    return added


def link_claim_evidence(db: Database, conn: sqlite3.Connection, pid: int) -> int:
    """Link top quantitative claims of this problem's source artifacts to each
    question (evidence_type='claim')."""
    qids = [r["id"] for r in conn.execute(
        "SELECT id FROM investigation_questions WHERE problem_id=? ORDER BY rank",
        (pid,))]
    claims = _claims_for_problem(conn, pid)
    if not claims or not qids:
        return 0
    scored = sorted(claims, key=lambda r: -_quantitative_score(r["proposition"]))
    chosen = scored[:10]
    linked = 0
    for qid in qids:
        for cl in chosen:
            dup = conn.execute(
                "SELECT 1 FROM question_evidence WHERE question_id=? AND "
                "evidence_type='claim' AND evidence_id=?", (qid, cl["id"])).fetchone()
            if not dup:
                try:
                    db.link_question_evidence(qid, "claim", cl["id"])
                    linked += 1
                except Exception:
                    pass
    conn.commit()
    return linked


def link_owid_domain_series(db: Database, conn: sqlite3.Connection, pid: int) -> int:
    """Attach already-ingested OWID measurements whose domain matches keywords
    in the problem statement."""
    prow = conn.execute("SELECT statement FROM problems WHERE id=?", (pid,)).fetchone()
    want = owid_domains_for(prow["statement"])
    slugs = [s for s, (dom, _l) in OWID_INDICATORS.items() if dom in want]
    if not slugs:
        return 0
    qids = [r["id"] for r in conn.execute(
        "SELECT id FROM investigation_questions WHERE problem_id=? ORDER BY rank",
        (pid,))]
    linked = 0
    like = ",".join("?" * len(slugs))
    mrows = conn.execute(
        f"SELECT id FROM measurements WHERE measurement_source IN ({like}) "
        f"AND reference_period >= '2020'",
        tuple(f"OWID:{s}" for s in slugs)).fetchall()
    # keep it lean: only the newest 2 series per slug
    per_slug = {}
    for m in mrows:
        row = conn.execute("SELECT measurement_source FROM measurements WHERE id=?",
                           (m["id"],)).fetchone()
        per_slug.setdefault(row["measurement_source"], []).append(m["id"])
    keep_ids = []
    for src_name, ids in per_slug.items():
        rows = sorted(ids, reverse=True)[:24]
        keep_ids.extend(rows)
    for qid in qids:
        for mid in keep_ids:
            dup = conn.execute(
                "SELECT 1 FROM question_evidence WHERE question_id=? AND "
                "evidence_type='measurement' AND evidence_id=?", (qid, mid)).fetchone()
            if not dup:
                try:
                    db.link_question_evidence(qid, "measurement", mid)
                    linked += 1
                except Exception:
                    pass
    conn.commit()
    return linked


def main() -> None:
    listing_only = "--list" in sys.argv
    db = Database("nexus_think_tank.db")
    db.init()
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row

    cands = conn.execute(
        "SELECT id, statement, iran_relevant, status FROM problems "
        "ORDER BY id").fetchall()

    promoted = []
    for p in cands:
        nq = conn.execute(
            "SELECT COUNT(*) FROM investigation_questions WHERE problem_id=?",
            (p["id"],)).fetchone()[0]
        nev = conn.execute(
            """SELECT COUNT(*) FROM question_evidence qe
               JOIN investigation_questions q ON q.id=qe.question_id
               WHERE q.problem_id=?""", (p["id"],)).fetchone()[0]
        print(f"P{p['id']} [{p['status']}] iran={p['iran_relevant']} "
              f"q={nq} ev={nev} | {p['statement'][:60]}")
        if listing_only:
            continue
        if p["status"] != "candidate" or p["iran_relevant"] != "yes":
            continue
        if nev == 0 and nq == 0:
            continue  # empty candidate; nothing to scaffold from
        added_q = scaffold_questions_if_missing(db, conn, p["id"])
        lc = link_claim_evidence(db, conn, p["id"])
        lo = link_owid_domain_series(db, conn, p["id"])
        total_ev = conn.execute(
            """SELECT COUNT(*) FROM question_evidence qe
               JOIN investigation_questions q ON q.id=qe.question_id
               WHERE q.problem_id=?""", (p["id"],)).fetchone()[0]
        if total_ev > 0:
            conn.execute("UPDATE problems SET status='ready' WHERE id=?", (p["id"],))
            conn.commit()
            promoted.append(p["id"])
            print(f"   -> READY (+q={added_q} +claims={lc} +owid={lo}, ev={total_ev})")
        else:
            print(f"   -> skipped (no linkable evidence)")

    conn.close()
    db.close()
    print(f"\nPROMOTED: {promoted}")


if __name__ == "__main__":
    main()
