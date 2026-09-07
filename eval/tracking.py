"""V3 of the vision: longitudinal problem tracking.

WHY THIS EXISTS
The pipeline was stateless across posts: pick problem -> pull evidence ->
compose -> publish -> forget. Nothing learned in run N reached run N+1 except an
"avoid these openings" ledger. So post 20 was no smarter than post 3, and the
system could never write the sentence that separates a research desk from a
content channel:

    «شش ماه پیش این عدد X بود؛ امروز Y است.»

This module records, per published post, the concrete metric readings that post
relied on. On a later run for the same problem it can then answer: what actually
changed since we last looked?

Design notes
- A snapshot stores the metric's canonical identity (from metric_registry) plus
  value + reference period, so comparison is semantic, not string matching.
- Comparison reports direction AND whether the change is meaningful for that
  metric kind (a 0.1pp move in a rate is noise; in a share it may not be).
- polarity from the registry tells us whether "up" is good or bad, so the desk
  can phrase honestly without an LLM judgement call.

Usage:
    python eval/tracking.py init
    python eval/tracking.py snapshot <pid>    # record current readings
    python eval/tracking.py changes <pid>     # what changed since last snapshot
    python eval/tracking.py report
"""
import sqlite3
import sys
from datetime import datetime, timezone

DB = "nexus_think_tank.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS problem_snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id    INTEGER NOT NULL,
    taken_at      TEXT NOT NULL,
    indicator     TEXT NOT NULL,
    canonical_id  TEXT,
    label_fa      TEXT,
    value         REAL NOT NULL,
    unit_fa       TEXT,
    ref_period    TEXT,
    polarity      TEXT,
    metric_kind   TEXT
);
CREATE INDEX IF NOT EXISTS idx_snap_problem
    ON problem_snapshots(problem_id, taken_at);
CREATE INDEX IF NOT EXISTS idx_snap_indicator
    ON problem_snapshots(problem_id, indicator);
"""

# Minimum absolute change worth mentioning, by metric kind. Below this a move is
# reporting noise, and claiming it as a trend would be dishonest.
NOISE_FLOOR = {"rate": 0.5, "share": 0.5, "index": 1.0, "level": 0.0,
               "count": 0.0}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _latest_readings(conn: sqlite3.Connection, pid: int) -> list[dict]:
    """The most recent value per indicator linked to this problem, joined to the
    metric registry so every reading carries its semantics."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.indicator,
               m.value,
               m.reference_period,
               m.provisional,
               r.canonical_id, r.label_fa, r.unit_fa, r.polarity, r.metric_kind
        FROM measurements m
        JOIN question_evidence qe
             ON qe.evidence_type='measurement' AND qe.evidence_id = m.id
        JOIN investigation_questions q ON q.id = qe.question_id
        LEFT JOIN metric_registry r ON r.indicator = m.indicator
        WHERE q.problem_id = ?
          AND (m.status IS NULL OR m.status != 'projection')
          AND m.reference_period = (
              SELECT MAX(m2.reference_period) FROM measurements m2
              WHERE m2.indicator = m.indicator)
        GROUP BY m.indicator
        ORDER BY m.indicator""", (pid,)).fetchall()
    return [dict(r) for r in rows]


def snapshot(conn: sqlite3.Connection, pid: int) -> int:
    init(conn)
    readings = _latest_readings(conn, pid)
    if not readings:
        print(f"P{pid}: no linked measurements -- nothing to snapshot")
        return 0
    now = datetime.now(timezone.utc).isoformat()
    for r in readings:
        conn.execute("""
            INSERT INTO problem_snapshots
              (problem_id, taken_at, indicator, canonical_id, label_fa,
               value, unit_fa, ref_period, polarity, metric_kind)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (pid, now, r["indicator"], r["canonical_id"], r["label_fa"],
             r["value"], r["unit_fa"], r["reference_period"],
             r["polarity"], r["metric_kind"]))
    conn.commit()
    print(f"P{pid}: snapshotted {len(readings)} indicator(s) at {now[:19]}")
    return len(readings)


def changes(conn: sqlite3.Connection, pid: int) -> list[dict]:
    """Compare current readings against the most recent PREVIOUS snapshot."""
    conn.row_factory = sqlite3.Row
    prev_at = conn.execute(
        "SELECT MAX(taken_at) FROM problem_snapshots WHERE problem_id=?",
        (pid,)).fetchone()[0]
    if not prev_at:
        print(f"P{pid}: no prior snapshot -- run `snapshot {pid}` first")
        return []
    prev = {r["indicator"]: dict(r) for r in conn.execute(
        "SELECT * FROM problem_snapshots WHERE problem_id=? AND taken_at=?",
        (pid, prev_at)).fetchall()}
    out = []
    for cur in _latest_readings(conn, pid):
        p = prev.get(cur["indicator"])
        if not p:
            out.append({"indicator": cur["indicator"],
                        "label_fa": cur["label_fa"], "status": "new",
                        "value": cur["value"], "unit_fa": cur["unit_fa"]})
            continue
        delta = cur["value"] - p["value"]
        kind = cur["metric_kind"] or "level"
        floor = NOISE_FLOOR.get(kind, 0.0)
        # for levels, treat a <1% relative move as noise
        if kind in ("level", "count"):
            meaningful = abs(delta) > abs(p["value"]) * 0.01
        else:
            meaningful = abs(delta) >= floor
        if abs(delta) == 0:
            direction = "unchanged"
        elif delta > 0:
            direction = "up"
        else:
            direction = "down"
        # polarity: is this direction good or bad for the subject?
        pol = cur["polarity"] or "neutral"
        if direction == "unchanged" or pol == "neutral":
            verdict = "neutral"
        elif (direction == "up" and pol == "good") or \
             (direction == "down" and pol == "bad"):
            verdict = "improved"
        else:
            verdict = "worsened"
        out.append({
            "indicator": cur["indicator"], "label_fa": cur["label_fa"],
            "status": "changed" if meaningful else "noise",
            "prev": p["value"], "prev_period": p["ref_period"],
            "value": cur["value"], "period": cur["reference_period"],
            "delta": delta, "direction": direction, "verdict": verdict,
            "unit_fa": cur["unit_fa"], "since": prev_at[:10],
        })
    return out


VERDICT_FA = {"improved": "بهبود", "worsened": "بدتر شدن", "neutral": "بی‌طرف"}


def describe_changes(rows: list[dict]) -> list[str]:
    """Persian one-liners for the meaningful changes only. These are FACTS for
    the writer to use, not prose to publish verbatim. Fully Persian: an English
    verdict token would leak into a Persian context pack."""
    out = []
    for r in rows:
        if r.get("status") != "changed":
            continue
        lbl = r.get("label_fa") or r["indicator"]
        unit = r.get("unit_fa") or ""
        arrow = "افزایش" if r["direction"] == "up" else "کاهش"
        verdict = VERDICT_FA.get(r.get("verdict", "neutral"), "")
        period = r.get("period") or ""
        prev_period = r.get("prev_period") or ""
        # only show the period span when the reference period actually moved;
        # "(2025→2025)" is noise that invites the reader to distrust the figure
        span = (f" ({prev_period}→{period})"
                if prev_period and period and prev_period != period else "")
        # P0 honesty: a provisional (modelled) reading is an estimate, not an
        # observed statistic; the writer must not frame it as one.
        prov = " (برآورد اولیه)" if r.get("provisional") else ""
        out.append(f"{lbl}: از {r['prev']:.6g} به {r['value']:.6g} {unit}"
                   f"{span}{prov} — {arrow}؛ {verdict}")
    return out


def report(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    init(conn)
    rows = conn.execute("""
        SELECT problem_id, COUNT(DISTINCT taken_at) snaps,
               COUNT(*) readings, MAX(taken_at) last
        FROM problem_snapshots GROUP BY problem_id ORDER BY problem_id""").fetchall()
    if not rows:
        print("no snapshots yet")
        return
    print(f"{'problem':9s} {'snapshots':10s} {'readings':9s} last")
    for r in rows:
        print(f"  P{r['problem_id']:<7d} {r['snaps']:<10d} {r['readings']:<9d} "
              f"{(r['last'] or '')[:19]}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    conn = sqlite3.connect(DB)
    if cmd == "init":
        init(conn)
        print("problem_snapshots ready")
    elif cmd == "snapshot":
        snapshot(conn, pid)
    elif cmd == "changes":
        rows = changes(conn, pid)
        if rows:
            for r in rows:
                if r.get("status") == "new":
                    print(f"  NEW   {r['label_fa'] or r['indicator']}: {r['value']:.6g}")
                elif r["status"] == "changed":
                    print(f"  CHG   {r['label_fa'] or r['indicator']}: "
                          f"{r['prev']:.6g} -> {r['value']:.6g} "
                          f"({r['direction']}, {r['verdict']})")
                else:
                    print(f"  noise {r['label_fa'] or r['indicator']}: "
                          f"delta={r['delta']:.6g}")
            print()
            for line in describe_changes(rows):
                print("  FA:", line)
    else:
        report(conn)
    conn.close()


if __name__ == "__main__":
    main()
