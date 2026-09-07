"""Lane B scenario runner (2026-09-05): build one scenario problem end to end.

Usage: python eval/make_scenario.py [--compose]
Without --compose: prepares problem + evidence, prints pid.
With --compose: also runs run_v03 (no-send) for the draft.
"""
import os
import sqlite3
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.database import Database  # noqa: E402

DB = "nexus_think_tank.db"

STATEMENT = ("What breaks first in Iran's oil-funded budget if Brent crude "
             "falls from its September 2026 level of $95.63 back to ~$70 "
             "per barrel.")
SCENARIO = ("ماشه: نفت برنت از ۹۵٫۶۳ دلار (سپتامبر ۲۰۲۶) به حدود ۷۰ دلار "
            "برگردد. سهم سوخت از صادرات کالایی ایران ۵۶٫۴٪ (۲۰۲۲) است. "
            "حساب کن چه مقدار درآمد نفتی کم می‌شود و اول به کجا فشار می‌آید. "
            "این یک فرض شرطی است، نه پیش‌بینی.")
IRAN_NOTE = ("Oil funds the budget; Brent carries a war premium now "
             "($72.92 Jun-2026 -> $95.63 Sep-2026). A snapback reprices "
             "everything fiscal.")


def main() -> int:
    db = Database(DB)
    db.init()  # runs the scenario-column migration idempotently
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    dup = conn.execute("SELECT id FROM problems WHERE statement=?",
                       (STATEMENT,)).fetchone()
    if dup:
        pid = dup["id"]
        print(f"scenario problem exists: P{pid}")
    else:
        pid = db.insert_problem(STATEMENT, polarity="problem",
                                topic_id=None, artifact_id=None)
        conn.execute("UPDATE problems SET source='scenario', iran_relevant='yes',"
                     " iran_note=?, scenario=? WHERE id=?",
                     (IRAN_NOTE, SCENARIO, pid))
        conn.commit()
        print(f"inserted P{pid}")
    from eval.discover import (draft_questions, question_gate, evidence_sweep,
                               link_series_by_topic)
    from eval.ask_gather import gather as _gather
    nq = conn.execute("SELECT COUNT(*) FROM investigation_questions "
                      "WHERE problem_id=?", (pid,)).fetchone()[0]
    if not nq:
        qs = draft_questions(STATEMENT, IRAN_NOTE)
        ok, reason = question_gate(qs)
        print(f"questions gate={'PASS' if ok else 'FAIL: ' + reason}")
        for i, qq in enumerate(qs[:3]):
            db.insert_investigation_question(pid, qq, rank=i)
    try:
        print("gather:", _gather(pid))
    except Exception as e:
        print(f"gather failed open: {type(e).__name__}")
    print("sweep:", evidence_sweep(conn, pid))
    print("series:", link_series_by_topic(conn, pid))
    nev = conn.execute(
        "SELECT COUNT(*) FROM question_evidence qe "
        "JOIN investigation_questions q ON q.id=qe.question_id "
        "WHERE q.problem_id=?", (pid,)).fetchone()[0]
    print(f"P{pid} evidence={nev}")
    if nev > 0:
        conn.execute("UPDATE problems SET status='ready' WHERE id=?", (pid,))
        conn.commit()
    conn.close()
    db.close()
    if "--compose" in sys.argv:
        p = subprocess.run(
            [sys.executable, "eval/run_v03.py", str(pid), "--no-send"],
            capture_output=True, text=True, timeout=1200)
        print(p.stdout[-2000:])
        print(p.stderr[-500:] if p.returncode else "compose exit=0")
        return p.returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
