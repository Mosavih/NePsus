"""V2 of the vision: repair OWID measurements (wrong-country + no-history).

WHY THIS EXISTS
Two bugs in fetch_owid() corrupted every OWID indicator whose grapher view is a
"latest value" snapshot:

  1. WRONG COUNTRY. `country=~IRN` is silently ignored by those endpoints, which
     return one row per country (195, alphabetical). The loop took rows
     unconditionally, so Afghanistan (first row) was stored labelled 'Iran'.
     Iran's unemployment is 8.3%; we published Afghanistan's 13.4%.
  2. NO HISTORY. `csvType=filtered` returns only that snapshot, so 8 indicators
     held exactly 1 data point and could never produce a trend chart.

fetch_owid() is now fixed (hard Iran filter + unfiltered CSV). This script
repairs the DATA already in the DB: it deletes the affected measurement rows,
re-fetches full corrected series, and re-links them to the same investigation
questions so existing problems keep their evidence.

Usage:
    python eval/repair_owid.py audit     # show what is wrong, change nothing
    python eval/repair_owid.py repair    # delete bad rows + re-ingest
"""
import sqlite3
import sys

sys.path.insert(0, ".")

DB = "nexus_think_tank.db"


def audit(conn: sqlite3.Connection) -> list[str]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT indicator,
                  COUNT(*) n,
                  COUNT(DISTINCT reference_period) yrs,
                  MIN(reference_period) lo, MAX(reference_period) hi
           FROM measurements
           WHERE measurement_source LIKE 'OWID:%'
           GROUP BY indicator ORDER BY yrs""").fetchall()
    suspect = []
    print(f"{'indicator':44s} rows  yrs  range")
    for r in rows:
        flag = "  <-- SNAPSHOT ONLY" if r["yrs"] <= 2 else ""
        print(f"  {r['indicator'][:42]:44s} {r['n']:4d} {r['yrs']:4d}  "
              f"{r['lo']}-{r['hi']}{flag}")
        if r["yrs"] <= 2:
            suspect.append(r["indicator"])
    if suspect:
        print(f"\n{len(suspect)} indicator(s) hold a snapshot instead of a "
              f"series -- these carry wrong-country values:")
        for s in suspect:
            print("  -", s)
    return suspect


def repair(conn: sqlite3.Connection) -> None:
    from src.investigation_layer.opendata import fetch_owid, OWID_INDICATORS
    conn.row_factory = sqlite3.Row

    # indicator label -> slug
    label_to_slug = {label: slug for slug, (_d, label) in OWID_INDICATORS.items()}
    suspect = audit(conn)
    if not suspect:
        print("\nnothing to repair")
        return

    print("\n--- repairing ---")
    for label in suspect:
        slug = label_to_slug.get(label)
        if not slug:
            print(f"  {label}: no slug mapping, skipped")
            continue

        # remember which questions cited this indicator so we can re-link
        qids = [r["question_id"] for r in conn.execute(
            """SELECT DISTINCT qe.question_id
               FROM question_evidence qe
               JOIN measurements m ON m.id = qe.evidence_id
               WHERE qe.evidence_type='measurement' AND m.indicator=?""",
            (label,)).fetchall()]

        try:
            fresh = fetch_owid(slug)
        except Exception as e:
            print(f"  {label}: fetch failed ({str(e)[:60]}) -- leaving old rows")
            continue

        old_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM measurements WHERE indicator=?", (label,)).fetchall()]
        conn.execute("""DELETE FROM question_evidence
                        WHERE evidence_type='measurement' AND evidence_id IN
                        (SELECT id FROM measurements WHERE indicator=?)""",
                     (label,))
        conn.execute("DELETE FROM measurements WHERE indicator=?", (label,))

        new_ids = []
        for row in fresh:
            cur = conn.execute(
                """INSERT INTO measurements
                     (indicator, subject_entity_name, value, unit,
                      reference_period, measurement_source, acquisition_method,
                      collection_time, status)
                   VALUES (?,?,?,?,?,?,?,datetime('now'),'raw')""",
                (row["indicator_name"], row["subject_entity"], row["value"],
                 row["unit"] or None, row["period_start"], row["source"],
                 "owid_csv"))
            new_ids.append(cur.lastrowid)

        # re-link every question that cited the indicator before
        links = 0
        for qid in qids:
            for mid in new_ids:
                conn.execute(
                    """INSERT INTO question_evidence
                         (question_id, evidence_type, evidence_id, created_at)
                       VALUES (?,'measurement',?,datetime('now'))""", (qid, mid))
                links += 1
        conn.commit()
        yrs = sorted(int(r["period_start"]) for r in fresh)
        print(f"  {label}: {len(old_ids)} bad row(s) -> {len(new_ids)} rows "
              f"({yrs[0]}-{yrs[-1]}), re-linked to {len(qids)} question(s) "
              f"[{links} links]")

    print("\n--- after repair ---")
    audit(conn)


def verify(conn: sqlite3.Connection) -> bool:
    """Standing integrity check: every OWID series must be Iran-only and hold
    real history. Run this after any ingestion. Exits non-zero on failure so it
    can gate a pipeline run."""
    conn.row_factory = sqlite3.Row
    bad = []
    rows = conn.execute(
        """SELECT indicator,
                  COUNT(DISTINCT reference_period) yrs,
                  COUNT(DISTINCT subject_entity_name) entities,
                  GROUP_CONCAT(DISTINCT subject_entity_name) names
           FROM measurements WHERE measurement_source LIKE 'OWID:%'
           GROUP BY indicator""").fetchall()
    for r in rows:
        if r["entities"] != 1 or (r["names"] or "") != "Iran":
            bad.append(f"{r['indicator']}: entities={r['names']} (expected Iran only)")
        if r["yrs"] <= 2:
            bad.append(f"{r['indicator']}: only {r['yrs']} year(s) -- snapshot, "
                       f"likely wrong-country latest-value view")
    if bad:
        print("INTEGRITY FAIL:")
        for b in bad:
            print("  -", b)
        return False
    print(f"INTEGRITY OK: {len(rows)} OWID indicators, Iran-only, all with history")
    return True


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    conn = sqlite3.connect(DB)
    ok = True
    if cmd == "repair":
        repair(conn)
    elif cmd == "verify":
        ok = verify(conn)
    else:
        audit(conn)
    conn.close()
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
