"""S1+S4: autonomous discovery — news -> problems -> specific questions -> evidence.

Replaces the per-cycle hand-written discover_p*_*.py scripts. One command:
    python eval/discover.py [--max-new 3] [--dry]

Stages:
  1. FETCH news via the existing feed (no new scraping).
  2. CLUSTER titles by shared content words; skip clusters overlapping
     existing problem themes (editorial memory: don't rediscover sanctions
     every cycle).
  3. DRAFT one problem statement per surviving cluster (LLM, EN, measurable).
  4. DRAFT 3 questions per problem (LLM, Persian) under S4 constraints.
  5. QUESTION GATE (mechanical, S4): reject generic templates
     ("در داده‌های کلان چگونه دیده می‌شود؟", "کدام ادعاها..."); require a
     named actor/mechanism/comparison; require >=1 non-macro question
     (institutional / historical / distributional markers). Failed problems
     stay status='candidate' with the reason recorded -- never silently ready.
  6. EVIDENCE SWEEP (codified linkers): claims via intake.link_claim_evidence,
     series via registry-topic match, findings via link_findings.link_problem.
  7. READY only with >=2 evidence substrates; else 'candidate'.

Idempotent: reruns skip existing statements (fuzzy match) and never duplicate
questions/links.
"""
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = "nexus_think_tank.db"

# S4: generic templates that guarantee generic posts. Banned.
BANNED_Q = [
    r"داده‌های کلان.*دیده می‌شود",
    r"کدام ادعاها",
    r"چگونه دیده می‌شود",
    r"چه تأثیری دارد\??$",
    r"وضعیت.*چگونه است\??$",
]
# S4: a question is SPECIFIC if it names one of these...
SPECIFIC_MARKERS = [
    # actors / institutions
    "دولت", "مجلس", "بانک مرکزی", "وزارت", "سپاه", "شهرداری", "صندوق",
    "اوپک", "آژانس", "شرکت", "بازار آزاد", "بخش خصوصی",
    # mechanisms
    "سازوکار", "از چه راهی", "چرا", "مکانیسم", "زنجیره",
    # comparisons / history / distribution
    "نسبت به", "در مقایسه", "گذشته", "دهه", "فقیر", "غنی", "روستا",
    "استان", "زنان", "جوانان", "نسل",
]
# ...and NON-MACRO if it carries one of these (institutional/historical/
# distributional -- the antidote to "another GDP post")
NONMACRO_MARKERS = [
    "دولت", "مجلس", "بانک مرکزی", "وزارت", "شهرداری", "قانون", "یارانه",
    "گذشته", "دهه", "تاریخ", "فقیر", "غنی", "روستا", "استان", "زنان",
    "توزیع", "سهم", "نابرابری", "فساد", "رانت",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_env() -> None:
    envp = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".env")
    for ln in open(envp, encoding="utf-8", errors="replace"):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, _, v = ln.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def _same_story(statement: str, recent: list[str]) -> bool:
    """Semantic dupe judge (2026-09-05): word overlap misses paraphrases of
    one news story (5 wedding-strike proposals at 0.3-0.4 overlap). One cheap
    FAST-tier call; fail-open (LLM error = not-a-dupe, the word gate already
    ran). Compares against the 15 most recent statements only."""
    if not recent:
        return False
    try:
        numbered = "\n".join(f"{i+1}. {s[:140]}" for i, s in enumerate(recent))
        txt = chat_resilient(
            [{"role": "user", "content":
                "Candidate research question:\n" + statement[:300] +
                "\n\nRecent questions:\n" + numbered +
                "\n\nIs the candidate the SAME news story/event as any listed"
                " one (same incident, same metric, same episode -- paraphrase"
                " counts)? Reply JSON only: {\"same\": true/false}"}],
            task="fast", temperature=0)
        import json as _j
        return bool(_j.loads(txt[txt.find("{"):txt.rfind("}") + 1]).get("same"))
    except Exception as e:
        print(f"  same-story check failed open: {type(e).__name__}")
        return False


def _words(s: str, minlen: int = 4) -> set:
    return {w for w in re.findall(r"[a-zA-Z\u0600-\u06FF]{%d,}" % minlen,
                                  (s or "").lower())}


# Domain map (2026-09-07C): discovery structurally favored the dominant
# political story, so drafting now spreads across domains. First match wins;
# anything else is "society".
DOMAINS: list[tuple[str, str]] = [
    ("conflict", r"strike|missile|casualt|killed|killing|war|military|retaliat|escalat|attack|troops|weapon|army|naval|drone"),
    ("economy", r"\boil\b|exports?|budget|\brial\b|currency|inflation|sanction|trade|tariff|subsid|bank|gdp|revenue"),
    ("tech", r"internet|cyber|\bai\b|software|satellite|telecom|mobile|startup|\bapp\b|hack|malware|shutdown|filtering|censor|satellite"),
    ("science", r"stud(y|ies)|research|vaccine|trial|discovery|scientists?| arxiv|genome|clinical|space|astronom"),
    ("environment", r"water|drought|\blake\b|pollution|\bair\b|sandstorm|earthquake|wildlife|forest|climate| urmia|dust|wetland"),
]
TECH_OUTLETS = {"Guardian Tech", "BBC Tech", "Guardian Science",
                "Guardian Environment"}


def classify_domain(text: str) -> str:
    t = (text or "").lower()
    for name, pat in DOMAINS:
        if re.search(pat, t):
            return name
    return "society"


def question_gate(questions: list) -> tuple[bool, str]:
    """S4 mechanical question gate. Returns (ok, reason)."""
    # the drafter sometimes returns dicts ({question, ...}); coerce
    qs = [q.get("question", q.get("text", str(q))) if isinstance(q, dict) else str(q)
          for q in questions]
    questions = qs
    if len(questions) < 3:
        return False, f"only {len(questions)} questions, need 3"
    for q in questions:
        for pat in BANNED_Q:
            if re.search(pat, q):
                return False, f"generic template: {q[:60]}"
    specific = sum(1 for q in questions
                   if any(m in q for m in SPECIFIC_MARKERS))
    if specific < 2:
        return False, "fewer than 2 specific questions"
    if not any(m in q for q in questions for m in NONMACRO_MARKERS):
        return False, "no non-macro question"
    return True, "ok"


def _router_client():
    _load_env()
    from openai import OpenAI
    from src.investigation_layer.models import model_for as _model_for
    base = os.environ.get("ROUTER_BASE_URL", "http://localhost:20128/v1")
    key = os.environ.get("ROUTER_API_KEY", "")
    # Combo edition: discovery drafting uses the draft combo. Explicit
    # ROUTER_MODEL still wins.
    model = os.environ.get("ROUTER_MODEL", _model_for("draft"))
    return (OpenAI(base_url=base, api_key=key,
                   timeout=float(os.environ.get("ROUTER_CALL_TIMEOUT", "300"))),
            model)


def chat_resilient(messages: list, task: str = "draft",
                   temperature: float = 0.3, timeout: float = 120.0,
                   max_tokens: int | None = None) -> str:
    """Shared resilient call for discover + ask + concept + datafirst.

    Paces (free-tier protection), then walks the task's combo with health
    ledger. Returns TEXT. Shared so every proposer path honors panel
    assignments and quarantines identically.
    """
    import time as _t
    from src.route_health import chat as _rchat
    _load_env()
    _t.sleep(12)
    text, _used = _rchat(task, messages, temperature=temperature,
                         timeout=timeout, max_tokens=max_tokens)
    return text


def draft_problem(cluster_titles: list[str]) -> dict | None:
    """One measurable problem statement (EN) + Iran note from a cluster."""
    txt = chat_resilient(
        [{"role": "user", "content":
            "You turn a news cluster into ONE research problem for an "
            "Iran-focused data-journalism desk. Reply JSON only: "
            '{"statement": "<one measurable EN sentence>", '
            '"iran_note": "<why Iran-relevant, EN>", '
            '"measurable": true/false}. '
            "RULES: the statement must name ONE single countable phenomenon "
            "(a price, a flow, a share, a rate) -- never bundle two or three "
            "different things with 'and' (e.g. 'sanction announcements, base "
            "attacks and diplomatic warnings' is THREE problems: pick the one "
            "the cluster is actually about); no war-casualty reporting, "
            "no pure diplomacy with nothing to measure; if the cluster is "
            "not measurable, set measurable=false. Prefer a specialized "
            "research-answerable angle (a mechanism, a technology, a "
            "measurable environmental or scientific process) over generic "
            "event reporting.\nCLUSTER:\n" +
            "\n".join(f"- {t}" for t in cluster_titles[:8])}],
        temperature=0.3)
    import json
    try:
        txt = txt[txt.find("{"):txt.rfind("}") + 1]
        return json.loads(txt)
    except Exception:
        return None


def draft_questions(statement: str, iran_note: str) -> list[str]:
    """3 specific Persian questions under S4 constraints."""
    txt = chat_resilient(
        [{"role": "user", "content":
            "برای یک میز داده‌روزنامه‌نگاری ایران، دقیقاً ۳ سؤال تحقیقی فارسی "
            "دربارهٔ این مسئله بنویس (JSON: {\"questions\": [..]}).\n"
            f"مسئله: {statement}\nربط به ایران: {iran_note}\n"
            "قوانین سخت:\n"
            "• هر سؤال باید یک بازیگر مشخص، یک سازوکار، یا یک مقایسه/دوره "
            "تاریخی نام ببرد (دولت، بانک مرکزی، یارانه، دههٔ گذشته، روستا "
            "در برابر شهر...).\n"
            "• دست‌کم یک سؤال غیرکلان باشد: نهادی، تاریخی یا توزیعی (چه کسی "
            "سود می‌برد/زیان می‌بیند؟ چه درسی از گذشته؟).\n"
            "• ممنوع: سؤال‌های کلی مثل «در داده‌های کلان چگونه دیده می‌شود؟» "
            "یا «کدام ادعاها آن را اندازه می‌گیرند؟».\n"
            "• هر سؤال یک جمله، حداکثر ۲۵ کلمه."}],
        temperature=0.5, task="draft")
    import json
    try:
        pass
        txt = txt[txt.find("{"):txt.rfind("}") + 1]
        raw = json.loads(txt).get("questions", []) or []
        out = []
        for q in raw:
            if isinstance(q, dict):
                q = q.get("question", q.get("text", ""))
            q = str(q or "").strip()
            if q:
                out.append(q)
        return out
    except Exception:
        return []


def evidence_sweep(conn: sqlite3.Connection, pid: int) -> dict:
    """Run the codified linkers; return substrate counts."""
    from src.database import Database
    import eval.intake as intake
    import eval.link_findings as lf
    db = Database(DB)
    db.init()
    counts = {"claim": 0, "measurement": 0, "finding": 0}
    try:
        counts["claim"] = intake.link_claim_evidence(db, conn, pid) or 0
    except Exception as e:
        print(f"  [sweep] claims failed: {type(e).__name__}")
    try:
        counts["measurement"] = link_series_by_topic(conn, pid) or 0
    except Exception as e:
        print(f"  [sweep] series failed: {type(e).__name__}")
    try:
        counts["finding"] = lf.link_problem(conn, pid) or 0
    except Exception as e:
        print(f"  [sweep] findings failed: {type(e).__name__}")
    # S3: per-problem scholarly retrieval (OpenAlex -> relevance gate ->
    # findings extracted + linked per question by investigate_problem itself).
    # This is what keeps the paper corpus growing with the news instead of
    # frozen at the August set.
    try:
        from src.investigation_layer.pipeline import investigate_problem
        summary = investigate_problem(db, pid, per_page=5)
        new_f = (summary.get("findings") or summary.get("finding_ids") or [])
        print(f"  [sweep] scholarly: {summary.get('studies', '?')} studies, "
              f"{len(new_f) if isinstance(new_f, list) else new_f} findings")
        # recount finding links (investigate_problem links them itself)
        counts["finding"] = conn.execute("""SELECT COUNT(*) FROM question_evidence qe
            JOIN investigation_questions q ON q.id=qe.question_id
            WHERE q.problem_id=? AND qe.evidence_type='finding'""",
            (pid,)).fetchone()[0]
    except Exception as e:
        print(f"  [sweep] scholarly failed: {type(e).__name__}: {str(e)[:80]}")
    db.close()
    return counts


GENERIC_IW = {"rate", "total", "annual", "goods", "services", "gross",
              "current", "share", "index", "growth", "per"}
# synonym families: the statement says 'crude oil', the indicator says
# 'fuel exports' -- same thing, zero shared tokens without this (live P22:
# the naive >=2 rule dropped EVERYTHING including exports).
FAMILIES = [
    {"export", "crude", "oil", "fuel", "petroleum", "gas", "trade"},
    {"reserve", "exchange", "rial", "dollar", "currency", "import"},
    {"price", "inflation", "cpi", "consumer"},
    {"water", "groundwater", "irrigation", "drought", "withdrawal"},
    {"energy", "electricity", "power", "grid", "renewable"},
    {"crop", "cereal", "wheat", "yield", "agriculture", "farm", "food",
     "land", "soil", "degradation", "dust", "desertification"},
    {"labor", "labour", "employment", "unemployment", "job", "women"},
    {"housing", "urban", "city", "cities", "vacancy", "mortgage"},
    {"population", "fertility", "birth", "marriage", "childbearing"},
    {"military", "militari", "defense", "defence", "army", "weapon",
     "expenditure"},
    {"conflict", "war", "battle", "death", "killed", "attack", "strike"},
    {"govern", "policy", "institution", "regulat", "subsid", "corrupt"},
]


def _stem(w: str) -> str:
    w = w.lower()
    return w[:-1] if w.endswith("s") and len(w) > 4 else w


def _fams(words: set) -> set:
    out = set()
    for i, fam in enumerate(FAMILIES):
        if words & fam:
            out.add(i)
    return out


def link_series_by_topic(conn: sqlite3.Connection, pid: int) -> int:
    """Link registry series whose topics match the problem statement's domains.

    Codifies the manual P16-18 linking: statement domains (via the same
    DOMAINS vocabulary as link_findings) intersect registry topics; link the
    last 26 points of each matching series to the problem's first question.
    Provisional/latest-only stubs with <5 points are skipped.
    """
    import eval.link_findings as lf
    prow = conn.execute("SELECT statement FROM problems WHERE id=?",
                        (pid,)).fetchone()
    if not prow:
        return 0
    doms = lf._domains(prow["statement"])
    if not doms:
        return 0
    q = conn.execute("""SELECT id FROM investigation_questions
        WHERE problem_id=? ORDER BY rank LIMIT 1""", (pid,)).fetchone()
    if not q:
        return 0
    # corroborate against statement + questions: the questions are part of
    # the problem's contract (live P22: narrow statement, broader legit questions
    # on inflation/reserves).
    qtext = " ".join(r["question"] for r in conn.execute(
        "SELECT question FROM investigation_questions WHERE problem_id=?",
        (pid,)))
    blob = (prow["statement"] + " " + qtext).lower()
    stmt_w = {_stem(w) for w in re.findall(r"[a-z]{4,}", blob)}
    stmt_f = _fams(stmt_w)
    n = 0
    # registry topics vs DOMAINS keys use different names ('agriculture' vs
    # 'agri') -- without this alias agriculture series never match any problem
    # (live: P15's own cereal evidence failed its own gate).
    _TALIAS = {"agriculture": "agri", "employment": "labour",
               "urbanization": "housing"}
    cands: list[tuple[int, str, list]] = []
    for r in conn.execute("SELECT indicator, topics FROM metric_registry"):
        rt = {_TALIAS.get(t.strip(), t.strip())
              for t in (r["topics"] or "").split(",")}
        if not (rt & doms):
            continue
        # corroboration: indicator's own words must meet the statement.
        # Domain-only matching put CO2 and cereal yield on a sanctions/oil
        # problem (77 hand-pruned links on P22) -- the prune_evidence lesson,
        # now at link time instead of cleanup time.
        iw = ({_stem(w) for w in re.findall(r"[a-z]{4,}", r["indicator"].lower())}
              - GENERIC_IW)
        overlap = (iw & stmt_w) | (_fams(iw) & stmt_f)
        if not overlap:
            continue
        mids = [x["id"] for x in conn.execute(
            """SELECT id FROM measurements WHERE indicator=?
               AND (status IS NULL OR status != 'projection')
               ORDER BY CAST(reference_period AS INT)""", (r["indicator"],))]
        # MONTHLY-PRECISION EXEMPTION (Phase A): high-frequency rows are sparse
        # by nature (1-2 points) -- the <5 stub-skip would permanently blind
        # the desk to the freshest observations (live P29: 88.6% June monthly
        # invisible, post led with 2025 annuals). Allow short series that
        # carry at least one YYYY-MM point.
        if len(mids) < 5:
            has_monthly = conn.execute(
                """SELECT 1 FROM measurements WHERE indicator=?
                   AND (status IS NULL OR status != 'projection')
                   AND reference_period LIKE '%-%' LIMIT 1""",
                (r["indicator"],)).fetchone()
            if not has_monthly:
                continue
        # THESIS CAP (2026-09-05): collect, don't attach yet. Uncapped
        # linking put military series on the online-business post
        # (P43) via question-word overlap, and the composer then HAD
        # to write its paragraph. Only the top-3 corroborated series
        # reach the composer.
        cands.append((len(overlap), r["indicator"], mids))
    cands.sort(key=lambda c: -c[0])
    for _, _ind, _mids in cands[:3]:
        for mid in _mids[-26:]:
            dup = conn.execute("""SELECT 1 FROM question_evidence
                WHERE question_id=? AND evidence_type='measurement'
                AND evidence_id=?""", (q["id"], mid)).fetchone()
            if not dup:
                conn.execute("""INSERT INTO question_evidence
                    (question_id, evidence_type, evidence_id, created_at)
                    VALUES (?, 'measurement', ?, ?)""",
                    (q["id"], mid, _now()))
                n += 1
    conn.commit()
    return n


def _store_feed_item(conn: sqlite3.Connection, it: dict):
    """Fetch a feed item's URL, extract text, store as pending artifact.

    Returns the artifact id or None. Uses Gate-1's trafilatura extractor;
    content-hash dedupe keeps reruns safe. Failures return None (the problem
    simply gets fewer claim substrates -- honest degradation)."""
    import hashlib
    from src.gate1_collection import _extract_article_text, _normalize_text
    url = (it.get("url") or "").strip()
    title = (it.get("title") or "").strip()
    if not url or not title:
        return None
    try:
        text = _extract_article_text(url)
    except Exception:
        return None
    if not text or len(text) < 200:
        return None
    h = hashlib.sha256(_normalize_text(text).encode()).hexdigest()
    dup = conn.execute("SELECT id FROM source_artifacts WHERE content_hash=?",
                       (h,)).fetchone()
    if dup:
        return dup["id"]
    cur = conn.execute("""INSERT INTO source_artifacts
        (source_id, publication_time, collection_time, original_title,
         original_content, original_url, language, media_type, content_hash,
         processing_status)
        VALUES (2, ?, ?, ?, ?, ?, 'en', 'text', ?, 'pending')""",
        (it.get("published") or _now(), _now(), title, text, url, h))
    conn.commit()
    print(f"  [store] a{cur.lastrowid}: {title[:60]}")
    return cur.lastrowid


def _extract_one(aid: int) -> int:
    """Extract Gate-2 claims for a single artifact. Returns claim count."""
    _load_env()
    import sqlite3 as _sq
    from src.gate2_extraction import _extract_all_llm
    c = _sq.connect(DB)
    c.row_factory = _sq.Row
    row = c.execute("""SELECT id, source_id, original_title, original_url,
        original_content, processing_status FROM source_artifacts
        WHERE id=?""", (aid,)).fetchone()
    if not row:
        c.close()
        return 0
    out = _extract_all_llm(
        (row["id"], row["source_id"], row["original_title"],
         row["original_url"], row["original_content"],
         row["processing_status"]))
    n = 0
    for cl in (out.get("claims") or []):
        prop = cl.get("claim_text") or cl.get("proposition") or ""
        if len(prop) < 25:
            continue
        c.execute("""INSERT INTO claims
            (artifact_id, proposition, claim_origin, claim_type,
             temporal_scope, time, status, created_at)
            VALUES (?,?,?,?,?,?, 'candidate', datetime('now'))""",
            (aid, prop, "source", cl.get("claim_type") or "fact",
             cl.get("temporal_scope") or "present",
             str(cl.get("time") or "None")))
        n += 1
    c.execute("UPDATE source_artifacts SET processing_status='extracted' "
              "WHERE id=?", (aid,))
    c.commit()
    c.close()
    time.sleep(12)
    return n


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-new", type=int, default=3)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    _load_env()
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from src.investigation_layer.news import fetch_news
    from src.database import Database as _DB
    from src.gate1_collection import collect_from_source
    # Gate 0: collect fresh articles into source_artifacts (pending).
    # Without this the feed titles have no stored content and no claims --
    # the failure mode that left P19-22 claimless.
    try:
        from src.models import Source as _Src
        _db0 = _DB(DB)
        _db0.init()
        _c0 = sqlite3.connect(DB)
        _c0.row_factory = sqlite3.Row
        for _r in _c0.execute("SELECT * FROM sources WHERE status='active'"):
            _s = _Src(id=_r["id"], name=_r["name"], url=_r["url"],
                      language=_r["language"] or "en",
                      country=_r["country"])
            try:
                collect_from_source(_s, _db0)
            except Exception as e:
                print(f"[collect] {_s.name} failed: {type(e).__name__}")
        _c0.close()
        _db0.close()
    except Exception as e:
        print(f"[collect] skipped: {type(e).__name__}: {str(e)[:80]}")
    # Gate 1.5: extract claims from pending artifacts (same machinery as
    # claims_backfill, inline so one command runs the whole front-end).
    try:
        from src.gate2_extraction import _extract_all_llm
        _conn1 = sqlite3.connect(DB)
        _conn1.row_factory = sqlite3.Row
        _pend = _conn1.execute("""SELECT id, source_id, original_title,
            original_url, original_content, processing_status
            FROM source_artifacts WHERE processing_status='pending'""").fetchall()
        print(f"[extract] {len(_pend)} pending artifact(s)")
        for _a in _pend:
            try:
                _out = _extract_all_llm(
                    (_a["id"], _a["source_id"], _a["original_title"],
                     _a["original_url"], _a["original_content"],
                     _a["processing_status"]))
                for _cl in (_out.get("claims") or []):
                    _prop = _cl.get("claim_text") or _cl.get("proposition") or ""
                    if len(_prop) < 25:
                        continue
                    _conn1.execute("""INSERT INTO claims
                        (artifact_id, proposition, claim_origin, claim_type,
                         temporal_scope, time, status, created_at)
                        VALUES (?,?,?,?,?,?, 'candidate', datetime('now'))""",
                        (_a["id"], _prop, "source",
                         _cl.get("claim_type") or "fact",
                         _cl.get("temporal_scope") or "present",
                         str(_cl.get("time") or "None")))
                _conn1.execute("""UPDATE source_artifacts
                    SET processing_status='extracted' WHERE id=?""", (_a["id"],))
                _conn1.commit()
            except Exception as e:
                print(f"[extract] a{_a['id']} failed: {type(e).__name__}")
                continue
            time.sleep(12)
        _conn1.close()
    except Exception as e:
        print(f"[extract] skipped: {type(e).__name__}: {str(e)[:80]}")
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    items = fetch_news()
    seen, titles, by_title = set(), [], {}
    for it in items:
        t = (it.get("title") or "").strip()
        if t and t not in seen:
            seen.add(t)
            titles.append(t)
            by_title[t] = it
    print(f"[discover] {len(titles)} distinct titles in feed")

    # cluster by shared content words (keep feed items: URL/outlet needed to
    # store cluster articles that Gate-1's 2 sources never collected)
    clusters: list[list[dict]] = []
    used = set()
    for i, t in enumerate(titles):
        if i in used:
            continue
        tw = _words(t)
        cl = [by_title[t]]
        used.add(i)
        for j, t2 in enumerate(titles):
            if j in used:
                continue
            if len(tw & _words(t2)) >= 3:
                cl.append(by_title[t2])
                used.add(j)
        if len(cl) >= 2:
            clusters.append(cl)
    # Niche tech/science stories rarely cluster (volume too low for 3 shared
    # words) yet are exactly the research-driven material the desk wants:
    # admit strong singletons from tech outlets as one-item clusters.
    for i, t in enumerate(titles):
        if i in used:
            continue
        it = by_title[t]
        if it.get("outlet") in TECH_OUTLETS:
            clusters.append([it])
            used.add(i)
    print(f"[discover] {len(clusters)} clusters (size>=2 + tech singletons)")

    # skip clusters overlapping existing problem themes
    existing = [r["statement"] for r in
                conn.execute("SELECT statement FROM problems")]
    exist_words = set()
    for s in existing:
        exist_words.update(_words(s, 5))
    fresh = []
    for cl in clusters:
        cw = set()
        for it in cl:
            cw.update(_words(it.get("title", ""), 5))
        overlap = len(cw & exist_words) / max(1, len(cw))
        blob = " ".join(it.get("title", "") for it in cl).lower()
        iran = "iran" in blob or "tehran" in blob or "persian" in blob
        if overlap < 0.45 and iran:
            fresh.append(cl)
    print(f"[discover] {len(fresh)} fresh Iran-relevant clusters")
    # DIVERSITY ORDER (2026-09-05 war-sort, 2026-09-07C scarcity): the
    # corpus structurally favors the dominant political story, so drafting
    # now (a) promotes domains scarcest among existing problems, (b) drafts
    # at most ONE problem per domain per run. A run may yield fewer than
    # max_new when the feed offers only one story -- that is honest output.
    _WAR = re.compile(
        r"(strike|strikes|missile|casualt|killed|killing|dead|death|war|"
        r"military|retaliat|escalat|attack|sanction|base|troops|defense|"
        r"defence|weapon|army)", re.I)
    scarcity: dict[str, int] = {}
    for s0 in existing:
        d0 = classify_domain(s0)
        scarcity[d0] = scarcity.get(d0, 0) + 1
    print(f"[discover] domain scarcity {scarcity}")
    fresh.sort(key=lambda cl: (
        scarcity.get(classify_domain(
            " ".join(it.get("title", "") or "" for it in cl)), 0),
        sum(len(_WAR.findall(it.get("title", "") or "")) for it in cl)))
    made = 0
    drafted_domains: set[str] = set()
    for cl in fresh[:args.max_new * 3]:
        if made >= args.max_new:
            break
        print(f"\n--- cluster: {cl[0].get('title','')[:80]} (+{len(cl)-1})")
        dom = classify_domain(" ".join(it.get("title", "") or "" for it in cl))
        if dom in drafted_domains:
            print(f"  skip: domain '{dom}' already drafted this run (spread rule)")
            continue
        d = draft_problem([it.get("title", "") for it in cl])
        if not d or not d.get("measurable") or not d.get("statement"):
            print("  skip: not measurable")
            continue
        # dedupe against existing statements (0.5: paraphrases of the same
        # story share fewer stems than exact dupes but still cluster -- live:
        # 5 wedding-strike proposals sailed through 0.6). The list grows
        # intra-run so same-batch dupes see each other (the old list loaded
        # once per run: P34 never saw P40).
        sw = _words(d["statement"], 5)
        if any(len(sw & _words(s, 5)) / max(1, len(sw)) > 0.5 for s in existing):
            print("  skip: duplicates an existing problem")
            continue
        if _same_story(d["statement"], existing[-15:]):
            print("  skip: same story as a recent problem (semantic)")
            continue
        qs = draft_questions(d["statement"], d.get("iran_note", ""))
        ok, reason = question_gate(qs)
        print(f"  questions ({len(qs)}): gate={'PASS' if ok else 'FAIL: ' + reason}")
        for q in qs:
            print(f"    - {q[:80]}")
        if args.dry:
            continue
        maxid = conn.execute("SELECT MAX(id) FROM problems").fetchone()[0] or 0
        pid = maxid + 1
        conn.execute("""INSERT INTO problems
            (id, statement, iran_relevant, iran_note, status, created_at)
            VALUES (?,?, 'yes', ?, ?, ?)""",
            (pid, d["statement"], d.get("iran_note", ""),
             "ready" if ok else "candidate", _now()))
        # S1: problems cite their source articles via problem_evidence --
        # without these rows _claims_for_problem finds nothing and the claims
        # substrate is always empty (live: all four P19-22 sweeps got 0).
        for it in cl:
            t = it.get("title", "")
            a = conn.execute("""SELECT id FROM source_artifacts
                WHERE original_title LIKE ? LIMIT 1""",
                (t[:60] + "%",)).fetchone()
            if not a and it.get("url"):
                # cluster article Gate-1 never stored (different outlet):
                # fetch + store + extract it now so the problem has claims
                nid = _store_feed_item(conn, it)
                if nid:
                    _extract_one(nid)
                    a = {"id": nid}
            if a:
                conn.execute("""INSERT OR IGNORE INTO problem_evidence
                    (problem_id, evidence_type, evidence_id)
                    VALUES (?, 'artifact', ?)""", (pid, a["id"]))
        conn.commit()  # release the write lock before Database() opens its own
        from src.database import Database
        db = Database(DB)
        db.init()
        for rank, q in enumerate(qs[:3], start=1):
            db.insert_investigation_question(pid, q, rank)
        db.close()
        existing.append(d["statement"])  # same-batch dupes must see this one
        drafted_domains.add(dom)
        if not ok:
            print(f"  P{pid} stored as candidate ({reason})")
            conn.commit()
            continue
        counts = evidence_sweep(conn, pid)
        n_sub = sum(1 for v in counts.values() if v > 0)
        print(f"  P{pid} evidence: {counts}")
        if n_sub < 2:
            conn.execute("UPDATE problems SET status='candidate' WHERE id=?",
                         (pid,))
            print(f"  P{pid} demoted to candidate (only {n_sub} substrate(s))")
        else:
            made += 1
            print(f"  P{pid} READY")
        conn.commit()
    conn.close()
    print(f"\n[discover] {made} new ready problem(s)")


if __name__ == "__main__":
    main()
