"""Codify research-finding linking into the pipeline (A-track, step 3).

The manual pass on 2026-09-02 established the rules this module encodes:
  1. Candidates: findings whose statement+title share >=2 content words with
     the problem statement (word-overlink >=3 was too loose: 'cities,land'
     linked a Dublin vacancy study to a dust-storm problem).
  2. Domain guard: a finding links to a problem only if its STUDY TITLE shares
     the problem's domain vocabulary (energy/water/land/economy...), not just
     generic words like 'cities' or 'growth'.
  3. Auto-link to the problem's first question (mechanism evidence slot).
  4. Idempotent: existing links are never duplicated.

Wired into run_v03 pre-compose: every problem gets its spine links refreshed
before composing, so the writer sees cited mechanisms without manual steps.

Usage: python eval/link_findings.py [pid ...]
"""
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "nexus_think_tank.db"

# Domain vocabulary: which study-title words belong to which problem domain.
DOMAINS = {
    "energy": {"energy", "renewable", "electricity", "grid", "power", "fuel"},
    "water": {"water", "groundwater", "hydro", "irrigation", "drought"},
    "agri": {"agricultur", "crop", "farm", "food", "soil", "land"},
    "climate": {"climate", "weather", "dust", "emission"},
    "economy": {"economy", "economic", "trade", "tariff", "sanction", "growth",
                "gdp", "investment", "credit", "macro", "inflat", "price",
                "prices", "cpi", "purchasing", "basket", "household",
                "market", "markets", "oil", "gold", "brent", "stock",
                "stocks", "share", "shares", "crude"},
    "housing": {"housing", "house", "mortgage", "urban", "city", "cities",
                "vacancy", "settlement"},
    "labour": {"labor", "labour", "employment", "unemployment", "women", "work"},
    "population": {"population", "fertility", "marriage", "family", "ageing",
                   "demograph"},
    "military": {"military", "militari", "defense", "defence", "army", "weapon",
                 "armed", "troop", "nato"},
    "conflict": {"conflict", "war", "battle", "casualt", "killed", "attack",
                 "strike", "violence"},
    "governance": {"govern", "policy", "policie", "institution", "regulat",
                   "subsid", "corrupt", "transparen", "democra"},
    "technology": {"technolog", "digital", "internet", "mobile", "broadband",
                   "telecom", "connectiv", "startup", "platform", "ai",
                   "cyber", "data", "software"},
}


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z]{4,}", (s or "").lower())}


def _domains(text: str) -> set:
    t = (text or "").lower()
    # FALSE-FRIEND GUARD (live P29): 'purchasing power' is not energy and
    # 'households' is not housing. Strip economics collocations before
    # matching, and require whole-word hits for short terms ('power' inside
    # 'manpower'/'willpower', 'house' inside 'households' must not fire).
    for phrase in ("purchasing power", "households", "household"):
        t = t.replace(phrase, " ")
    out = set()
    for dom, terms in DOMAINS.items():
        for term in terms:
            if len(term) < 6:
                if re.search(r"\b" + re.escape(term) + r"\b", t):
                    out.add(dom)
                    break
            elif term in t:
                out.add(dom)
                break
    return out


def primary_domain(text: str) -> str | None:
    """A study's PRIMARY domain = the domain whose terms appear most often in
    its title. 'The Groundwater-Energy-Food Nexus...' mentions water-terms most
    -> water study, not an energy study, even though 'energy' appears. This
    stopped subsidized-water findings being linked to the grid-blackout problem
    (manual judgment of 2026-09-02, codified)."""
    t = (text or "").lower()
    best, best_n = None, 0
    for dom, terms in DOMAINS.items():
        n = sum(t.count(term) for term in terms)
        if n > best_n:
            best, best_n = dom, n
    return best


def link_problem(conn: sqlite3.Connection, pid: int, min_overlap: int = 2,
                 dry: bool = False) -> int:
    now = datetime.now(timezone.utc).isoformat()
    prow = conn.execute("SELECT statement FROM problems WHERE id=?", (pid,)).fetchone()
    if not prow:
        return 0
    stmt_words = _words(prow["statement"])
    stmt_domains = _domains(prow["statement"])
    if not stmt_domains:
        return 0
    qid = conn.execute("""SELECT id FROM investigation_questions
        WHERE problem_id=? ORDER BY rank LIMIT 1""", (pid,)).fetchone()
    if not qid:
        return 0
    qid = qid["id"]
    n = 0
    for f in conn.execute("""SELECT f.id, f.statement, s.title FROM findings f
        JOIN studies s ON s.id=f.study_id
        WHERE COALESCE(s.evidence_quality,'')<>'rejected'"""):
        fw = _words(f["statement"] + " " + f["title"])
        overlap = fw & stmt_words
        pdom = primary_domain(f["title"])
        # CALIBRATED GATE (2026-09-02 manual review, codified):
        #   primary domain must match AND (overlap>=3 OR Iran-specific with
        #   overlap>=2). overlap 0-1 with a matching domain is coincidence --
        #   51 false links were created before this rule landed.
        if pdom not in stmt_domains:
            continue
        if not (len(overlap) >= 3 or
                (len(overlap) >= 2 and
                 any(k in (f["statement"] + " " + f["title"]).lower()
                     for k in ("iran", "iranian", "middle east", "west asia")))):
            continue
        ex = conn.execute("""SELECT 1 FROM question_evidence
            WHERE question_id=? AND evidence_type='finding' AND evidence_id=?""",
            (qid, f["id"])).fetchone()
        if ex:
            continue
        if not dry:
            conn.execute("""INSERT INTO question_evidence
                (question_id, evidence_type, evidence_id, created_at)
                VALUES (?, 'finding', ?, ?)""", (qid, f["id"], now))
        n += 1
        print(f"  P{pid} <- finding {f['id']} (domain {pdom}, "
              f"overlap {sorted(overlap)[:4]})")
    return n


def main() -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    pids = [int(a) for a in sys.argv[1:]] or \
        [r["id"] for r in conn.execute(
            "SELECT id FROM problems WHERE status IN ('ready','published')")]
    total = 0
    for pid in pids:
        n = link_problem(conn, pid)
        if n:
            print(f"P{pid}: +{n} research-finding link(s)")
        total += n
    conn.commit()
    conn.close()
    print(f"total: {total} new links")


if __name__ == "__main__":
    main()
