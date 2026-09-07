"""Live-ish retrieval for user-asked problems: search the stored article
corpus by the problem's own keywords (Google News links don't resolve
server-side; GDELT throttles shared IPs). The corpus holds 70+ full-text
recent articles, so this is retrieval, not recycling: keyword-matched
articles get claim-extracted and linked like any news cluster's would be.

Usage: python eval/ask_gather.py <pid>
"""
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

STOP = {"what", "which", "with", "from", "that", "this", "these", "those",
        "have", "there", "their", "about", "among", "after", "before",
        "percentage", "percent", "rate", "total", "monthly", "standard",
        "following", "iran", "iranian", "among",
        # GENERIC-NOUN GUARD (live P39): 'people' linked wedding-strike
        # claims to a broadband problem; 'power' pulls energy articles.
        # Gather keywords must be distinctive or they are worse than none.
        "people", "person", "persons", "household", "households", "change",
        "changed", "changes", "cost", "costs", "group", "groups", "impact",
        "effects", "power", "small", "large", "country"}


def keywords(statement: str) -> list[str]:
    words = re.findall(r"[a-z]{5,}", (statement or "").lower())
    seen, out = set(), []
    for w in words:
        if w not in STOP and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def gather(pid: int, top_n: int = 6) -> dict:
    from eval.discover import _extract_one, _now
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row
    prow = conn.execute("SELECT statement FROM problems WHERE id=?",
                        (pid,)).fetchone()
    if not prow:
        print(f"no problem {pid}")
        return {}
    kws = keywords(prow["statement"])
    print(f"[gather] keywords: {kws}")
    scored = []
    for a in conn.execute(
            """SELECT id, original_title, processing_status,
                      LENGTH(original_content) clen
               FROM source_artifacts WHERE LENGTH(original_content) > 500"""):
        blob = ((a["original_title"] or "") + " ").lower()
        hits = sum(1 for k in kws if k in blob)
        # Require 2+ keyword hits: a single generic-word hit is how wedding
        # claims ended up on a broadband problem (live P39).
        if hits >= 2:
            # content match weighs double
            scored.append((hits, a["id"], a["original_title"][:70],
                           a["processing_status"]))
    scored.sort(reverse=True)
    linked, extracted = 0, 0
    for hits, aid, title, status in scored[:top_n]:
        if status != "extracted":
            try:
                extracted += _extract_one(aid) or 0
            except Exception as e:
                print(f"  [gather] extract a{aid} failed: {type(e).__name__}")
        conn.execute("""INSERT OR IGNORE INTO problem_evidence
            (problem_id, evidence_type, evidence_id)
            VALUES (?, 'artifact', ?)""", (pid, aid))
        linked += 1
        print(f"  [gather] ({hits}) a{aid}: {title}")
    conn.commit()
    conn.close()
    return {"articles": linked, "claims_extracted": extracted}


if __name__ == "__main__":
    print(gather(int(sys.argv[1])))
