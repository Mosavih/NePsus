"""V4 of the vision: cross-problem synthesis + dated, scorable forecasts.

WHY THIS EXISTS
Every post so far answers one problem in isolation. A think tank earns authority
two ways a content channel cannot fake:

  1. SYNTHESIS -- saying something true that requires looking at several problems
     at once ("the water constraint in P3 is the same constraint capping the
     export target in P4").
  2. ACCOUNTABILITY -- issuing dated, falsifiable predictions and later scoring
     itself against them. The `forecasts` table has the right shape
     (resolution_criteria, evaluation_status, evaluation_score) and zero rows.

HOW LINKS ARE FOUND (honestly)
Entity linking was tried first and rejected: the only entity shared across
problems is "Iran", which carries no signal -- the same ubiquity trap that
defeated the earlier relevance guard. Instead links are derived from the METRIC
REGISTRY: each problem inherits the `topics` of the metrics attached to it, and
two problems are neighbours when they share enough NON-GENERIC topics.

Generic topics (economy/growth/welfare) are down-weighted because almost every
Iranian problem touches them; they cannot distinguish a real relationship.

Usage:
    python eval/synthesis.py links              # candidate problem pairs
    python eval/synthesis.py forecast <pid>     # draft a dated forecast
    python eval/synthesis.py due                # forecasts ready to resolve
    python eval/synthesis.py score <fid> <hit|miss|partial> "<note>"
    python eval/synthesis.py record             # scoring record so far
"""
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

DB = "nexus_think_tank.db"

# Topics almost every Iranian problem touches -- they do not evidence a real
# relationship between two problems, so they score less.
GENERIC_TOPICS = {"economy", "growth", "welfare", "prices"}

# English terms evidencing a topic in a problem STATEMENT. Attached metrics alone
# are not proof of subject: the generic OWID bundle is linked to many problems, so
# P6 (a NATO/military problem) inherited energy+climate topics and looked like a
# neighbour of the energy problem -- the same contamination that once put a CO2
# chart on a military post. A topic only counts when the problem's own statement
# corroborates it.
TOPIC_EVIDENCE = {
    "agriculture": ["agricultur", "farm", "crop", "greenhouse", "wheat", "cereal"],
    "food": ["food", "essential goods", "nutrition"],
    "water": ["water", "drought", "irrigat"],
    "energy": ["energy", "fuel", "gas", "power"],
    "electricity": ["electric", "power", "grid", "blackout"],
    "climate": ["climate", "emission", "carbon", "warming"],
    "environment": ["environment", "pollut", "emission"],
    "labour": ["labor", "labour", "employ", "work", "job"],
    "employment": ["employ", "job", "unemploy", "work"],
    "youth": ["youth", "young"],
    "gender": ["women", "gender", "female"],
    "wages": ["wage", "salary", "income"],
    "housing": ["housing", "house", "rent", "dwelling", "town"],
    "urbanization": ["urban", "city", "town"],
    "population": ["population", "demograph", "childbearing", "birth"],
    "trade": ["export", "import", "trade", "sanction", "tariff"],
    "inflation": ["inflation", "price"],
}


def topic_supported_by_statement(topic: str, statement: str) -> bool:
    """Does the problem's own statement evidence this topic? Unknown topics pass
    (fail-open) so a newly added topic is not silently dropped."""
    terms = TOPIC_EVIDENCE.get(topic)
    if not terms:
        return True
    s = (statement or "").lower()
    return any(t in s for t in terms)


def problem_topics(conn: sqlite3.Connection,
                   require_statement: bool = True) -> dict:
    """Topics per problem, inherited from attached registered metrics but
    CORROBORATED by the problem's own statement when require_statement is set."""
    conn.row_factory = sqlite3.Row
    statements = {r["id"]: r["statement"] for r in conn.execute(
        "SELECT id, statement FROM problems")}
    out = defaultdict(set)
    for r in conn.execute("""
            SELECT DISTINCT q.problem_id pid, r.topics
            FROM measurements m
            JOIN question_evidence qe
                 ON qe.evidence_type='measurement' AND qe.evidence_id = m.id
            JOIN investigation_questions q ON q.id = qe.question_id
            JOIN metric_registry r ON r.indicator = m.indicator"""):
        for t in (r["topics"] or "").split(","):
            t = t.strip()
            if not t:
                continue
            if require_statement and not topic_supported_by_statement(
                    t, statements.get(r["pid"], "")):
                continue
            out[r["pid"]].add(t)
    return dict(out)


def find_links(conn: sqlite3.Connection, min_score: float = 1.0) -> list[dict]:
    """Candidate problem pairs, scored by shared SPECIFIC topics.
    Specific topic = 1.0, generic topic = 0.25.

    Threshold note: statements are short, so after statement-corroboration a real
    relationship may rest on ONE specific topic (P3 energy/water vs P4
    agricultural exports both keep 'agriculture'). Requiring two specific topics
    silently discarded genuine links, so one specific topic qualifies -- but a
    pair built only from GENERIC topics never does."""
    conn.row_factory = sqlite3.Row
    pt = problem_topics(conn)
    titles = {r["id"]: r["statement"] for r in conn.execute(
        "SELECT id, statement FROM problems")}
    pairs = []
    pids = sorted(pt)
    for i, a in enumerate(pids):
        for b in pids[i + 1:]:
            shared = pt[a] & pt[b]
            if not shared:
                continue
            specific = sorted(t for t in shared if t not in GENERIC_TOPICS)
            score = sum(1.0 if t not in GENERIC_TOPICS else 0.25
                        for t in shared)
            if score >= min_score and specific:
                pairs.append({
                    "a": a, "b": b, "score": round(score, 2),
                    "specific": specific,
                    "shared": sorted(shared),
                    "a_title": (titles.get(a) or "")[:80],
                    "b_title": (titles.get(b) or "")[:80],
                })
    pairs.sort(key=lambda p: -p["score"])
    return pairs


# ---------------------------------------------------------------- forecasts
def _client():
    from openai import OpenAI
    import os
    base = os.environ.get("ROUTER_BASE_URL", "")
    key = os.environ.get("ROUTER_API_KEY", "")
    if not base or not key:
        envp = ".env"
        if os.path.exists(envp):
            for line in open(envp, encoding="utf-8", errors="replace"):
                s = line.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                if k.strip() == "ROUTER_BASE_URL" and not base:
                    base = v.strip()
                if k.strip() == "ROUTER_API_KEY" and not key:
                    key = v.strip()
    return OpenAI(base_url=base or "http://localhost:20128/v1",
                  api_key=key or "sk-x")


# MODEL removed: forecast now uses task cards combo via route_health.chat


def latest_readings(conn: sqlite3.Connection, pid: int) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.indicator, m.value, m.reference_period,
               r.label_fa, r.unit_fa, r.polarity, r.metric_kind
        FROM measurements m
        JOIN question_evidence qe
             ON qe.evidence_type='measurement' AND qe.evidence_id = m.id
        JOIN investigation_questions q ON q.id = qe.question_id
        JOIN metric_registry r ON r.indicator = m.indicator
        WHERE q.problem_id = ?
          AND m.reference_period = (
              SELECT MAX(m2.reference_period) FROM measurements m2
              WHERE m2.indicator = m.indicator)
        GROUP BY m.indicator ORDER BY m.indicator""", (pid,)).fetchall()
    return [dict(r) for r in rows]


def trend_for(conn: sqlite3.Connection, indicator: str, n: int = 6) -> list:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT reference_period yr, value FROM measurements
        WHERE indicator=? ORDER BY CAST(reference_period AS INT) DESC
        LIMIT ?""", (indicator, n)).fetchall()
    return [(r["yr"], r["value"]) for r in reversed(rows)]


def draft_forecast(conn: sqlite3.Connection, pid: int,
                   horizon_months: int = 12) -> dict | None:
    """Draft ONE falsifiable forecast for a problem, grounded in its own series.

    Discipline: the prediction must name the metric, a direction, a threshold and
    a date, so it can be scored later without argument. A forecast that cannot be
    proven wrong is worthless."""
    readings = latest_readings(conn, pid)
    if not readings:
        print(f"P{pid}: no registered metrics -- cannot forecast")
        return None
    # pick the metric with the most history and a real trend
    best, best_trend = None, None
    for r in readings:
        tr = trend_for(conn, r["indicator"])
        if len(tr) >= 4:
            if best is None or len(tr) > len(best_trend or []):
                best, best_trend = r, tr
    if not best:
        print(f"P{pid}: no metric with enough history")
        return None

    hist = "، ".join(f"{y}: {v:.4g}" for y, v in best_trend)
    target = (datetime.now(timezone.utc)
              + timedelta(days=30 * horizon_months)).date().isoformat()
    prompt = (
        "تو تحلیلگر یک اتاق فکر داده‌محور هستی. بر پایه روند واقعی زیر، "
        "یک پیش‌بینی «قابل ابطال» بنویس.\n"
        f"شاخص: {best['label_fa']} (واحد: {best['unit_fa'] or '-'})\n"
        f"روند: {hist}\n"
        f"افق: تا تاریخ {target}\n\n"
        "قواعد سخت:\n"
        "- پیش‌بینی باید شاخص، جهت (افزایش/کاهش) و یک آستانه عددی مشخص داشته باشد.\n"
        "- باید بتوان بعداً با داده، درست یا غلط بودنش را ثابت کرد.\n"
        "- محتاط باش: بر اساس روند، نه آرزو.\n"
        "خروجی دقیقاً به این فرمت (سه خط):\n"
        "پیش‌بینی: ...\n"
        "معیار داوری: ...\n"
        "احتمال: <عدد بین 0.5 و 0.95>")
    try:
        from src.route_health import chat as _rchat
        txt, _used = _rchat("synthesis",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=320, pace=12.0)
        txt = (txt or "").strip()
    except Exception as e:
        print(f"P{pid}: forecast call failed: {type(e).__name__}: {str(e)[:80]}")
        return None

    pred, crit, prob = "", "", 0.6
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("پیش‌بینی:"):
            pred = s.split(":", 1)[1].strip()
        elif s.startswith("معیار داوری:"):
            crit = s.split(":", 1)[1].strip()
        elif s.startswith("احتمال:"):
            try:
                prob = float(s.split(":", 1)[1].strip().replace("٫", "."))
            except ValueError:
                pass
    if not pred or not crit:
        print(f"P{pid}: forecast unparseable -- discarded")
        return None
    # a forecast with no number cannot be scored
    import re as _re
    if not _re.search(r"[\d۰-۹]", pred):
        print(f"P{pid}: forecast has no threshold -- discarded (unfalsifiable)")
        return None
    prob = min(max(prob, 0.5), 0.95)

    cur = conn.execute("""
        INSERT INTO forecasts
          (prediction, probability, target_horizon, conditions,
           resolution_criteria, date_issued, author, evaluation_status)
        VALUES (?,?,?,?,?,?,?, 'unresolved')""",
        (pred, prob, target,
         f"problem_id={pid}; metric={best['indicator']}; baseline={best_trend[-1][1]}",
         crit, datetime.now(timezone.utc).date().isoformat(), "nexus-desk"))
    conn.commit()
    fid = cur.lastrowid
    print(f"P{pid}: forecast #{fid} issued (p={prob}, resolve by {target})")
    print(f"  {pred}")
    print(f"  معیار: {crit}")
    return {"id": fid, "prediction": pred, "criteria": crit,
            "probability": prob, "target": target}


def due(conn: sqlite3.Connection) -> list[dict]:
    conn.row_factory = sqlite3.Row
    today = datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute("""
        SELECT * FROM forecasts
        WHERE evaluation_status='unresolved' AND target_horizon <= ?
        ORDER BY target_horizon""", (today,)).fetchall()
    return [dict(r) for r in rows]


def score(conn: sqlite3.Connection, fid: int, outcome: str, note: str) -> None:
    """Record how a forecast actually turned out. hit=1.0, partial=0.5, miss=0.0.
    Scoring yourself is the point: an unscored forecast is just an opinion."""
    val = {"hit": 1.0, "partial": 0.5, "miss": 0.0}.get(outcome)
    if val is None:
        print("outcome must be hit|partial|miss")
        return
    conn.execute("""UPDATE forecasts
                    SET outcome=?, evaluation_status='resolved', evaluation_score=?
                    WHERE id=?""", (note or outcome, val, fid))
    conn.commit()
    print(f"forecast #{fid} scored {outcome} ({val})")


def record(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM forecasts ORDER BY id").fetchall()
    if not rows:
        print("no forecasts issued yet")
        return
    resolved = [r for r in rows if r["evaluation_status"] == "resolved"]
    print(f"forecasts: {len(rows)} issued, {len(resolved)} resolved")
    if resolved:
        acc = sum(r["evaluation_score"] or 0 for r in resolved) / len(resolved)
        print(f"track record: {acc:.2f} mean score")
    for r in rows:
        mark = {"resolved": "✓", "unresolved": "…"}.get(r["evaluation_status"], "?")
        print(f"  {mark} #{r['id']} p={r['probability']:.2f} "
              f"by {r['target_horizon']}: {r['prediction'][:78]}")


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "links"
    conn = sqlite3.connect(DB)
    if cmd == "links":
        for p in find_links(conn):
            print(f"  P{p['a']} & P{p['b']}  score={p['score']}  "
                  f"specific={p['specific']}")
            print(f"      A: {p['a_title']}")
            print(f"      B: {p['b_title']}")
    elif cmd == "forecast":
        draft_forecast(conn, int(sys.argv[2]))
    elif cmd == "due":
        d = due(conn)
        if not d:
            print("nothing due for resolution")
        for r in d:
            print(f"  #{r['id']} due {r['target_horizon']}: {r['prediction'][:80]}")
    elif cmd == "score":
        score(conn, int(sys.argv[2]), sys.argv[3],
              sys.argv[4] if len(sys.argv) > 4 else "")
    else:
        record(conn)
    conn.close()


if __name__ == "__main__":
    main()
