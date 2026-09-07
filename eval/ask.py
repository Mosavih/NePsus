"""Phase E: custom user questions into the same gates as news problems.

Usage: python eval/ask.py "<user text>" [--compose]

1. LLM turns the text into ONE measurable EN problem statement (+ Iran note).
2. draft_questions + question_gate (same S4 rules as news problems).
3. evidence_sweep (claims + series + findings + scholarly per-problem run).
4. READY only on >=2 substrates (or >=1 solution lever when the user asks
   "what can be done"); else print exactly what is missing and stop --
   never a fabricated post.
5. --compose runs the standard run_v03 compose WITHOUT sending (delivery
   happens only through the review bot's approve button).

Problems get source='user-ask' so news/user mixes stay measurable.
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from eval.discover import (draft_questions, evidence_sweep, question_gate)  # noqa: E402
from src.database import Database  # noqa: E402

DB = "nexus_think_tank.db"


def _progress(stage: str) -> None:
    try:
        import json
        with open("var/ask_progress.json", "w", encoding="utf-8") as f:
            json.dump({"stage": stage, "ts": time.time()}, f)
    except Exception:
        pass


def ensure_source_col(conn: sqlite3.Connection) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(problems)")]
    if "source" not in cols:
        conn.execute("ALTER TABLE problems ADD COLUMN source TEXT DEFAULT 'news'")
        conn.execute("UPDATE problems SET source='news' WHERE source IS NULL")
        conn.commit()


def draft_problem_from_user(text: str) -> dict | None:
    from eval.discover import chat_resilient
    txt = chat_resilient([{"role": "user", "content":
            "You turn a reader's question into ONE research problem for an "
            "Iran-focused data-journalism desk. Reply JSON only: "
            '{"statement": "<one measurable EN sentence>", '
            '"iran_note": "<why Iran-relevant, EN>", '
            '"measurable": true/false, '
            '"wants_solutions": true/false (does the reader ask what can be done?)}. '
            "RULES: the statement must name something countable (a price, a flow, "
            "a share, a rate); if the text is not measurable, set measurable=false.\n"
            f"READER TEXT:\n{text[:600]}"}],
        temperature=0.3, task="ask")
    import json
    try:
        return json.loads(txt[txt.find("{"):txt.rfind("}") + 1])
    except Exception:
        return None


def main() -> int:
    text = sys.argv[1] if len(sys.argv) > 1 else ""
    compose = "--compose" in sys.argv
    if not text.strip():
        print("usage: python eval/ask.py \"<question>\" [--compose]")
        return 2
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row  # linkers index rows by column name
    ensure_source_col(conn)
    # dedupe against existing statements
    from eval.discover import _words
    sw = _words(text, 5)
    existing = [r[0] for r in conn.execute("SELECT statement FROM problems")]
    if any(len(sw & _words(s, 5)) / max(1, len(sw)) > 0.6 for s in existing):
        print("ASK: duplicates an existing problem -- use that one instead")
        return 1
    d = draft_problem_from_user(text)
    if not d or not d.get("measurable") or not d.get("statement"):
        print("ASK: not measurable -- no post (honest stop)")
        return 1
    _progress("drafting questions")
    qs = draft_questions(d["statement"], d.get("iran_note", ""))
    ok, reason = question_gate(qs)
    print(f"ASK: questions ({len(qs)}): gate={'PASS' if ok else 'FAIL: ' + reason}")
    for q in qs:
        print(f"  - {q[:90]}")
    if not ok:
        return 1
    db = Database(DB)
    db.init()
    pid = db.insert_problem(d["statement"], polarity="problem", topic_id=None,
                            artifact_id=None)
    conn.execute("""UPDATE problems SET source='user-ask', iran_relevant='yes',
                    iran_note=? WHERE id=?""", (d.get("iran_note", ""), pid))
    conn.commit()
    for i, q in enumerate(qs):
        db.insert_investigation_question(pid, q, rank=i)
    db.close()
    # Live retrieval: keyword-search the stored article corpus for THIS
    # problem (fixed feeds never cover arbitrary user questions; Google News
    # links don't resolve server-side). Articles get claim-extracted + linked
    # before the sweep counts substrates.
    try:
        from eval.ask_gather import gather as _gather
        _progress("searching saved articles")
        print(f"ASK: gather: {_gather(pid)}")
    except Exception as e:
        print(f"ASK: gather failed: {type(e).__name__} -- continuing")
    _progress("sweeping evidence")
    counts = evidence_sweep(conn, pid)
    n_sub = (counts.get("measurement", 0) > 0) + \
        (counts.get("finding", 0) > 0) + (counts.get("claim", 0) > 0)
    print(f"ASK: substrates {counts} (distinct kinds: {n_sub})")
    levers = conn.execute(
        """SELECT COUNT(*) FROM problem_interventions pi
           JOIN interventions i ON i.id=pi.intervention_id
           WHERE pi.problem_id=?
             AND (i.status IS NULL OR i.status != 'quarantined')""",
        (pid,)).fetchone()[0]
    ready = n_sub >= 2
    if not ready:
        # measurements across 3+ distinct indicators are a numbers backbone
        # on their own (live P29: inflation question, zero claims/findings,
        # but 8+ CPI/price series) -- provided they are the RIGHT topic,
        # which link_series_by_topic guarantees post-P29-false-friend fix.
        n_ind = conn.execute(
            """SELECT COUNT(DISTINCT m.indicator) FROM question_evidence qe
               JOIN investigation_questions q ON q.id=qe.question_id
               JOIN measurements m ON m.id=qe.evidence_id
               WHERE q.problem_id=? AND qe.evidence_type='measurement'""",
            (pid,)).fetchone()[0]
        ready = n_ind >= 3
    if d.get("wants_solutions") and not levers:
        print(f"ASK: P{pid} note: no solution levers -- post will quantify "
              f"only, no prescriptions (clause 12)")
    if not ready:
        missing = []
        if not counts.get("measurement"):
            missing.append("no quantitative series matched")
        if not counts.get("finding"):
            missing.append("no paper findings linked")
        if not counts.get("claim"):
            missing.append("no news claims linked")
        if d.get("wants_solutions") and not levers:
            missing.append("no solution levers in the bank")
        print(f"ASK: P{pid} stays candidate -- missing: {'; '.join(missing)}")
        conn.close()
        return 1
    conn.execute("UPDATE problems SET status='ready' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    print(f"ASK: P{pid} READY")
    if compose:
        _progress("composing post")
        import subprocess
        p = subprocess.run([sys.executable, "eval/run_v03.py", str(pid),
                            "--no-send"], capture_output=True, text=True,
                           timeout=590)
        print((p.stdout or "")[-800:])
    _progress("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
