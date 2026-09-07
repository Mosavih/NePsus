"""Data-first proposals: start from registry movements, not news clusters.

Why: news-driven discovery never proposes tech (tech news is rarely
Iran-tagged), so the tech pool sits unused. This scans a topic's series for
the largest multi-year movement, drafts ONE singular problem from the facts,
then runs the same gates + sweep as everything else.

Usage: python eval/propose_datafirst.py [topic]  (default: technology)
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from eval.discover import _now, _words, draft_questions, evidence_sweep, question_gate  # noqa: E402
from src.database import Database  # noqa: E402

DB = "nexus_think_tank.db"


def movements(conn: sqlite3.Connection, topic: str) -> list[dict]:
    out = []
    for r in conn.execute(
            "SELECT indicator FROM metric_registry WHERE topics LIKE ?",
            (f"%{topic}%",)):
        pts = conn.execute(
            """SELECT reference_period, value FROM measurements
               WHERE indicator=? AND (status IS NULL OR status != 'projection')
               AND reference_period NOT LIKE '%-%'
               ORDER BY reference_period""", (r["indicator"],)).fetchall()
        if len(pts) < 6:
            continue
        (p0, v0), (p1, v1) = pts[0], pts[-1]
        if not v0:
            continue
        out.append({"indicator": r["indicator"], "from": p0, "to": p1,
                    "v0": v0, "v1": v1, "pct": (v1 - v0) / abs(v0) * 100})
    out.sort(key=lambda d: -abs(d["pct"]))
    return out


def draft_problem_from_movement(m: dict) -> dict | None:
    from eval.discover import chat_resilient
    txt = chat_resilient([{"role": "user", "content":
            "You write ONE research problem for an Iran-focused "
            "data-journalism desk from this data movement. Reply JSON only: "
            '{"statement": "<one measurable EN sentence naming the metric>", '
            '"iran_note": "<why it matters for Iran, EN>"}. RULES: ONE single '
            "phenomenon; name the metric in plain words (internet use, mobile "
            "subscriptions); no 'and'-bundles.\nMOVEMENT: "
            f"{m['indicator']} in Iran moved {m['v0']:g} ({m['from']}) -> "
            f"{m['v1']:g} ({m['to']}), {m['pct']:+.1f}%."}],
        temperature=0.3, task="draft")
    import json
    try:
        pass
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:
        return None


def main() -> int:
    topic = sys.argv[1] if len(sys.argv) > 1 else "technology"
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    movs = movements(conn, topic)
    if not movs:
        print(f"no movements for topic {topic}")
        return 1
    existing = [r[0] for r in conn.execute("SELECT statement FROM problems")]
    for m in movs[:3]:
        d = draft_problem_from_movement(m)
        if not d or not d.get("statement"):
            continue
        sw = _words(d["statement"], 5)
        if any(len(sw & _words(s, 5)) / max(1, len(sw)) > 0.6 for s in existing):
            print(f"  skip (duplicate): {d['statement'][:70]}")
            continue
        qs = draft_questions(d["statement"], d.get("iran_note", ""))
        ok, reason = question_gate(qs)
        print(f"  {d['statement'][:80]} gate={'PASS' if ok else 'FAIL: ' + reason}")
        if not ok:
            continue
        db = Database(DB)
        db.init()
        pid = db.insert_problem(d["statement"], polarity="problem",
                                topic_id=None, artifact_id=None)
        conn.execute("""UPDATE problems SET source='data-first',
                        iran_relevant='yes', iran_note=? WHERE id=?""",
                     (d.get("iran_note", ""), pid))
        conn.commit()
        for i, q in enumerate(qs):
            db.insert_investigation_question(pid, q, rank=i)
        db.close()
        try:
            from eval.ask_gather import gather as _gather
            _gather(pid)
        except Exception as e:
            print(f"  gather failed: {type(e).__name__}")
        counts = evidence_sweep(conn, pid)
        nev = conn.execute(
            """SELECT COUNT(*) FROM question_evidence qe
               JOIN investigation_questions q ON q.id=qe.question_id
               WHERE q.problem_id=?""", (pid,)).fetchone()[0]
        if nev > 0:
            conn.execute("UPDATE problems SET status='ready' WHERE id=?", (pid,))
            conn.commit()
            print(f"  P{pid} READY ev={nev} {counts}")
        else:
            print(f"  P{pid} stays candidate (no evidence)")
        conn.close()
        return 0
    conn.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
