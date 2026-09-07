"""V5 of the vision: the research-paper spine (and its quality gate).

WHY THIS EXISTS
`studies` held 121 rows and `findings` 54, but posts leaned on news + macro
stats. Papers are what let a post explain a MECHANISM ("why does this happen?")
rather than only quantities ("how big is it?"), and they are the natural source
of the specialised vocabulary the glossary exists to explain.

WHAT THE AUDIT FOUND FIRST
The corpus was polluted. Alongside genuinely useful work (housing bubbles,
food-safety border inspections, urban vacancy) sat a plasma-physics camera paper,
a 1951 protein assay, and an image-recognition paper -- retrieval noise with a
sharp signature: a citation-count cliff (801,217 -> 1,141). Worse, 7 findings had
been EXTRACTED from those papers ("residual networks are easier to optimize"),
though they were linked to no problem so never reached a post.

Lesson: an unaudited corpus silently lowers the ceiling on every post that draws
from it. So this module gates quality first, then serves mechanisms.

Usage:
    python eval/papers.py audit           # corpus quality report
    python eval/papers.py clean           # quarantine off-domain studies
    python eval/papers.py mechanisms <pid>  # citable mechanisms for a problem
"""
import json
import sqlite3
import sys

DB = "nexus_think_tank.db"

# A paper cited this heavily is a foundational method paper (ResNet, Lowry
# assay), never a source about Iran's economy. This is a strong, cheap signal.
CITATION_CLIFF = 150_000

# Domain vocabulary: a study must plausibly touch one of the desk's subjects.
# NOTE: the first draft of this list produced FALSE POSITIVES -- "China's Real
# Estate Market", "Mortgage Credit Availability and Residential Construction" and
# "NATO and the future of European security" are all genuinely relevant to the
# desk's problems but were flagged off-domain because the list lacked real-estate,
# mortgage and security vocabulary. A quality gate that discards good evidence is
# worse than none, so the list must stay generous and the citation cliff does the
# heavy lifting.
DOMAIN_TERMS = [
    "hous", "urban", "vacan", "city", "cities", "rent", "resident",
    "real estate", "property", "mortgage", "construction", "dwelling", "land",
    "water", "drought", "irrigat", "groundwater", "aquifer", "soil",
    "energy", "electric", "power", "fuel", "gas", "renewab", "greenhouse",
    "cultivat", "photosynth", "yield", "harvest",
    "employ", "unemploy", "labor", "labour", "job", "wage", "workforce",
    "women", "gender", "female", "fertil", "marriage", "family",
    "population", "demograph", "migrat", "youth", "refugee",
    "agricultur", "farm", "crop", "wheat", "pistachio", "food", "export",
    "import", "trade", "tariff", "sanction", "inflation", "price", "market",
    "cooperativ", "welfare", "poverty", "inequality", "credit", "econom",
    "security", "conflict", "military", "nato", "defence", "defense",
    "iran", "middle east", "developing", "emerging", "china",
    "settlement", "infrastructur", "spatial", "planning", "climate",
]


def _is_on_domain(title: str, abstract: str, concepts: str) -> bool:
    blob = " ".join([(title or ""), (abstract or "")[:600],
                     (concepts or "")]).lower()
    return any(t in blob for t in DOMAIN_TERMS)


def audit(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""SELECT id, title, abstract, concepts, year,
                                  cited_by_count FROM studies""").fetchall()
    over_cited, off_domain, ok = [], [], []
    for r in rows:
        if (r["cited_by_count"] or 0) > CITATION_CLIFF:
            over_cited.append(r)
        elif not _is_on_domain(r["title"], r["abstract"], r["concepts"]):
            off_domain.append(r)
        else:
            ok.append(r)
    orphan = conn.execute("""
        SELECT COUNT(*) FROM findings f
        WHERE NOT EXISTS (SELECT 1 FROM question_evidence qe
                          WHERE qe.evidence_type='finding'
                            AND qe.evidence_id = f.id)""").fetchone()[0]
    print(f"studies: {len(rows)}")
    print(f"  on-domain           : {len(ok)}")
    print(f"  over-cited (method) : {len(over_cited)}")
    print(f"  off-domain          : {len(off_domain)}")
    print(f"findings not linked to any problem: {orphan}")
    if over_cited:
        print("\nover-cited (foundational method papers, not evidence):")
        for r in over_cited:
            print(f"  {r['cited_by_count']:>9,}  {(r['title'] or '')[:64]}")
    if off_domain:
        print("\noff-domain sample:")
        for r in off_domain[:8]:
            print(f"  - {(r['title'] or '')[:70]}")
    return {"ok": len(ok), "over_cited": [r["id"] for r in over_cited],
            "off_domain": [r["id"] for r in off_domain], "orphan": orphan}


def clean(conn: sqlite3.Connection) -> None:
    """Quarantine contaminated studies and the findings extracted from them.

    We mark rather than DELETE: evidence_quality='rejected' keeps provenance so a
    later audit can see what was excluded and why. Findings from rejected studies
    are removed only when no problem cites them."""
    conn.row_factory = sqlite3.Row
    a = audit(conn)
    bad = list(a["over_cited"]) + list(a["off_domain"])
    if not bad:
        print("\nnothing to quarantine")
        return
    print(f"\n--- quarantining {len(bad)} study/studies ---")
    qs = ",".join("?" * len(bad))
    conn.execute(f"UPDATE studies SET evidence_quality='rejected' "
                 f"WHERE id IN ({qs})", bad)
    # drop orphan findings sourced from rejected studies
    cur = conn.execute(f"""
        DELETE FROM findings WHERE study_id IN ({qs})
          AND NOT EXISTS (SELECT 1 FROM question_evidence qe
                          WHERE qe.evidence_type='finding'
                            AND qe.evidence_id = findings.id)""", bad)
    conn.commit()
    print(f"marked {len(bad)} studies rejected; removed {cur.rowcount} "
          f"orphan finding(s) extracted from them")
    kept = conn.execute("""SELECT COUNT(*) FROM findings f
                           JOIN studies s ON s.id=f.study_id
                           WHERE s.evidence_quality='rejected'""").fetchone()[0]
    if kept:
        print(f"NOTE: {kept} finding(s) from rejected studies are cited by a "
              f"problem and were kept -- review manually before reuse")


def mechanisms(conn: sqlite3.Connection, pid: int, limit: int = 4) -> list[dict]:
    """Citable mechanism statements for a problem, from non-rejected studies.

    A mechanism answers WHY, so causal findings are preferred: those carrying an
    intervention, an outcome or an effect size, from a study that survived the
    quality gate."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT f.statement, f.effect_size, f.causal_strength,
               f.geographic_applicability, f.limitations,
               s.title, s.year, s.doi, s.landing_url,
               COALESCE(s.evidence_quality,'') q
        FROM findings f
        JOIN studies s ON s.id = f.study_id
        JOIN question_evidence qe
             ON qe.evidence_type='finding' AND qe.evidence_id = f.id
        JOIN investigation_questions iq ON iq.id = qe.question_id
        WHERE iq.problem_id = ? AND COALESCE(s.evidence_quality,'') <> 'rejected'
        ORDER BY (f.effect_size IS NOT NULL) DESC,
                 (f.intervention_name IS NOT NULL) DESC,
                 s.year DESC""", (pid,)).fetchall()
    # De-duplicate: the same finding is often linked through several questions,
    # and duplicate rows would burn the small mechanism budget on one sentence.
    seen, out = set(), []
    for r in rows:
        key = (r["statement"] or "").strip()[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
        if len(out) >= limit:
            break
    return out


def mechanism_block(rows: list[dict]) -> str:
    """Persian context block: mechanism + citation, so the writer can explain WHY
    with attribution instead of only reciting quantities."""
    if not rows:
        return ""
    out = ["سازوکارها از پژوهش‌های داوری‌شده (با ارجاع؛ عدد جدید نساز):"]
    for r in rows:
        cite = f"{(r.get('title') or '')[:90]}"
        if r.get("year"):
            cite += f" ({r['year']})"
        line = f"- {(r.get('statement') or '')[:220]} — منبع: {cite}"
        if r.get("geographic_applicability"):
            line += f" [دامنه: {str(r['geographic_applicability'])[:40]}]"
        out.append(line)
    return "\n".join(out)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "audit"
    conn = sqlite3.connect(DB)
    if cmd == "clean":
        clean(conn)
    elif cmd == "mechanisms":
        pid = int(sys.argv[2])
        rows = mechanisms(conn, pid)
        if not rows:
            print(f"P{pid}: no citable mechanisms")
        else:
            print(mechanism_block(rows))
    else:
        audit(conn)
    conn.close()


if __name__ == "__main__":
    main()
