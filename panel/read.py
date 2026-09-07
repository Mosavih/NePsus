"""Read-only accessors over the live Nexus system. NEVER writes to live paths.

All functions open the live DB in read-only mode and read var/posts files
without modifying them. Panel-owned state lives in panel/state.db (state.py).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = ROOT / "nexus_think_tank.db"
VAR = ROOT / "var"
POSTS = ROOT / "posts"
SOURCES_YAML = ROOT / "sources.yaml"


def open_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- sources ---
def yaml_sources() -> list[dict]:
    """Minimal hand-parse of sources.yaml (name/url/type/language/country)."""
    out, cur = [], {}
    try:
        lines = SOURCES_YAML.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if s.startswith("- name:"):
            if cur:
                out.append(cur)
            cur = {"name": s.split(":", 1)[1].strip().strip('"')}
        elif cur and ":" in s and not s.startswith("#"):
            k, v = s.split(":", 1)
            k = k.strip()
            if k in ("url", "type", "language", "country", "collection_method"):
                cur[k] = v.strip().strip('"')
    if cur:
        out.append(cur)
    return out


def sources_status() -> list[dict]:
    """Join yaml sources x DB sources x latest artifact per source."""
    rows = []
    try:
        conn = open_ro()
    except sqlite3.Error:
        return [{"name": s.get("name", "?"), "error": "live DB unavailable"} for s in yaml_sources()]
    try:
        db_sources = {r["name"]: dict(r) for r in conn.execute("SELECT * FROM sources")}
        for s in yaml_sources():
            name = s.get("name", "?")
            dbs = db_sources.get(name, {})
            latest = None
            if dbs:
                r = conn.execute(
                    """SELECT MAX(collection_time) AS t, COUNT(*) AS n FROM source_artifacts
                       WHERE source_id = ?""",
                    (dbs.get("id"),),
                ).fetchone()
                if r and r["n"]:
                    latest = {"at": r["t"], "n": r["n"]}
            rows.append({
                "name": name, "url": s.get("url", ""), "type": s.get("type", ""),
                "language": s.get("language", ""), "country": s.get("country", ""),
                "db_status": dbs.get("status", "not-in-db"),
                "latest": latest,
            })
    finally:
        conn.close()
    return rows


# ------------------------------------------------------------------ funnel ---
def funnel_counts() -> dict:
    conn = open_ro()
    try:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
        return {
            "problems": q("SELECT COUNT(*) FROM problems"),
            "problems_by_status": [tuple(r) for r in
                conn.execute("SELECT status, COUNT(*) FROM problems GROUP BY status")],
            "questions": q("SELECT COUNT(*) FROM investigation_questions"),
            "questions_open": q("SELECT COUNT(*) FROM investigation_questions WHERE status != 'answered'"),
            "studies": q("SELECT COUNT(*) FROM studies"),
            "findings": q("SELECT COUNT(*) FROM findings"),
            "measurements": q("SELECT COUNT(*) FROM measurements"),
            "events": q("SELECT COUNT(*) FROM events"),
            "artifacts": q("SELECT COUNT(*) FROM source_artifacts"),
            "claims": q("SELECT COUNT(*) FROM claims"),
        }
    finally:
        conn.close()


# ------------------------------------------------------------------- update ---
def update_state() -> dict:
    ask = _read_json(VAR / "ask_progress.json", {}) or {}
    delivery = _read_json(VAR / "last_delivery.json", {}) or {}
    pid_alive, pid = False, None
    try:
        pid = int((VAR / "bot.pid").read_text(encoding="utf-8").strip().split()[0])
        os.kill(pid, 0)
        pid_alive = True
    except (OSError, ValueError):
        pass
    logs = sorted(VAR.glob("prog_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    log_tails = []
    for lp in logs:
        try:
            lines = lp.read_text(encoding="utf-8", errors="replace").splitlines()
            log_tails.append({"file": lp.name, "mtime": lp.stat().st_mtime, "tail": lines[-8:]})
        except OSError:
            pass
    return {"ask": human_ask(ask), "delivery": human_delivery(delivery),
            "bot_pid": pid, "bot_alive": pid_alive, "recent_logs": log_tails}


def human_ask(ask: dict) -> str:
    if not ask:
        return "No pipeline activity recorded yet."
    stage = ask.get("stage", "working")
    ts = ask.get("ts")
    when = _rel_ts(float(ts)) if ts else "recently"
    return f"{stage} — {when}."


def human_delivery(delivery: dict) -> str:
    if not delivery:
        return "Nothing delivered to the channel yet."
    parts = []
    for path, info in delivery.items():
        n = len((info or {}).get("ids", []))
        name = Path(path).stem.replace("_", " ").replace("post ", "Post ")
        parts.append(f"{name} published as {n} message(s)")
    return "; ".join(parts) + "."


def _rel_ts(ts: float) -> str:
    from datetime import datetime, timezone
    try:
        age = (datetime.now(timezone.utc).timestamp() - float(ts))
    except (TypeError, ValueError):
        return "recently"
    if age < 3600:
        return f"{int(age / 60)} min ago"
    if age < 86400 * 2:
        return f"{int(age / 3600)} hours ago"
    return f"{int(age / 86400)} days ago"


def job_progress() -> dict:
    """Live worker progress for the ops console. No worker -> running False."""
    from actions import MARKERS, worker_state
    st = worker_state()
    if st["pid"] is None or not st["alive"]:
        return {"running": False}
    cmd = st.get("cmd") or ""
    action = "work"
    for key in ("run-update", "run-propose", "run-compose"):
        if key in cmd:
            action = key
            break
    logs = sorted(VAR.glob("prog_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    text = ""
    started = None
    if logs:
        try:
            text = logs[0].read_text(encoding="utf-8", errors="replace")
            started = logs[0].stat().st_ctime
        except OSError:
            pass
    markers = MARKERS.get(action, [])
    hits = sum(1 for m in markers if m in text)
    last = [l for l in text.strip().splitlines() if l.strip()][-1:] or [""]
    import time as _t
    elapsed = int(_t.time() - started) if started else 0
    return {"running": True, "action": action, "cmd": cmd,
            "frac": hits / max(1, len(markers)), "step": f"{hits}/{len(markers)}",
            "last_line": last[0][:160], "elapsed": elapsed,
            "log": logs[0].name if logs else ""}


def _rel_time(ts) -> str:
    if not ts:
        return "never"
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age = (now - dt).total_seconds() / 3600
        if age < 1:
            return f"{int(age * 60)} min ago"
        if age < 48:
            return f"{int(age)} h ago"
        return f"{int(age / 24)} days ago"
    except ValueError:
        return str(ts)


# ---------------------------------------------------------------- channels ---
def channels() -> list[dict]:
    """Effective evidence channels, not just the yaml/DB list."""
    out = []
    feeds = _parse_live_feeds()
    conn = open_ro()
    try:
        latest_artifact = conn.execute(
            "SELECT MAX(collection_time) FROM source_artifacts").fetchone()[0]
        n_studies = conn.execute("SELECT COUNT(*) FROM studies").fetchone()[0]
        latest_measure = conn.execute(
            "SELECT MAX(collection_time) FROM measurements").fetchone()[0]
    finally:
        conn.close()
    if feeds:
        out.append({"name": "Live news feeds", "detail": ", ".join(sorted(feeds)),
                    "role": "what is happening now (headlines only, attributed)",
                    "freshness": ("latest stored article " + _rel_time(latest_artifact)
                                  if latest_artifact else "fetched fresh every run")})
    gd = _read_json(VAR / "gdelt_cache.json", {}) or {}
    out.append({"name": "GDELT", "detail": f"{len(gd)} cached queries",
                "role": "global event context per investigation",
                "freshness": "queried per run, cached 6 months"})
    out.append({"name": "Research papers (OpenAlex, Crossref, arXiv)",
                "detail": f"{n_studies} studies stored",
                "role": "scientific evidence behind findings",
                "freshness": "fetched per investigation"})
    out.append({"name": "Official statistics (World Bank, IMF)",
                "detail": "WDI + IMF macro series",
                "role": "numeric backbone of every post",
                "freshness": ("newest observation " + _rel_time(latest_measure)
                              if latest_measure else "refreshed on /update")})
    return out


def _parse_live_feeds() -> dict:
    """Feed names from the news layer source (no pipeline import — regex only)."""
    import re as _re
    try:
        text = (ROOT / "src" / "investigation_layer" / "news.py").read_text(encoding="utf-8")
    except OSError:
        return {}
    m = _re.search(r"FEEDS\s*=\s*\{([^}]*)\}", text)
    if not m:
        return {}
    return dict(_re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', m.group(1)))


# -------------------------------------------------------------------- posts ---
def posts_queue() -> list[dict]:
    """Composed posts discovered from posts/ + the live DB.

    manifest.json was the old source but compose never writes it, so
    finished posts vanished. Filesystem is the truth: any post_NN_v*.txt
    with a problem row in the live DB is a post.
    """
    scores = latest_scores()
    delivered = _read_json(VAR / "bot_notified.json", {}) or {}
    drafts = _read_json(VAR / "panel_drafts.json", {}) or {}
    out = []
    for txt in sorted(POSTS.glob("post_*_v*.txt")):
        name = txt.stem  # post_52_v4
        try:
            pid = int(name.split("_")[1])
        except (IndexError, ValueError):
            continue
        sc = scores.get(pid, {})
        out.append({
            "problem_id": pid,
            "decision": (drafts.get(str(pid)) or {}).get("decision", ""),
            "delivered": str(pid) in delivered,
            "sent_to_bot": str(pid) in drafts,
            "score": sc.get("total"),
            "model": sc.get("model"),
            "latest_txt": txt.name,
            "card": (POSTS / (txt.name.replace(".txt", "_card.png"))).name
            if (POSTS / txt.name.replace(".txt", "_card.png")).exists() else None,
            "versions": len(list(POSTS.glob(f"post_{pid}_v*.txt"))),
        })
    # dedupe by pid (keep newest version = last after sort)
    seen, dedup = set(), []
    for row in reversed(out):
        if row["problem_id"] in seen:
            continue
        seen.add(row["problem_id"])
        dedup.append(row)
    return list(reversed(dedup))


def problems_queue() -> list[dict]:
    """Every problem in the live DB, newest first.

    Rejected/archived filtering happens in the template (it has the panel
    decisions); this accessor stays a plain read so tests can assert on
    the full list.
    """
    conn = open_ro()
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(problems)")]
        pick = ["id", "statement", "status"]
        if "source" in cols:
            pick.append("source")
        if "created_at" in cols:
            pick.append("created_at")
        rows = conn.execute(
            f"SELECT {', '.join(pick)} FROM problems ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def latest_scores() -> dict:
    """Latest score_ledger row per problem. NOTE: `tier` holds the model name."""
    conn = open_ro()
    try:
        rows = conn.execute(
            "SELECT * FROM score_ledger ORDER BY pid, at").fetchall()
    finally:
        conn.close()
    latest, rounds = {}, {}
    for r in rows:
        d = dict(r)
        rounds.setdefault(d["pid"], []).append(d)
        latest[d["pid"]] = {"model": d.get("tier"), "total": d.get("total"),
                            "at": d.get("at"), "criteria": {
                                k: d.get(k) for k in
                                ("honesty", "thesis", "scenario_logic", "persian",
                                 "attribution", "hook")},
                            "notes": d.get("notes"), "rounds": len(rounds[d["pid"]])}
    latest["_rounds"] = rounds
    return latest


def parse_post(text: str) -> dict:
    """Split a v3/v4 post file into human sections. Falls back to raw body."""
    import ast as _ast
    import re as _re
    out: dict = {"version": "", "angle": "", "tldr": "", "glossary": "",
                 "outline": [], "body": [], "gates": [], "qc": {}, "note": ""}
    m = _re.match(r"\s*===\s*(.*?)\s*===\s*\n", text)
    rest = text
    if m:
        out["version"] = m.group(1)
        rest = text[m.end():]
    parts = _re.split(r"\n===\s*(GATES|QC|NOTE)\s*===\n", rest)
    head, tail = parts[0], parts[1:]
    body_lines: list[str] = []
    in_outline = False
    for line in head.splitlines():
        s = line.strip()
        if s.startswith("ANGLE:"):
            out["angle"] = s[6:].strip()
        elif s.startswith("TLDR:"):
            out["tldr"] = s[5:].strip()
        elif s.startswith("GLOSSARY:"):
            out["glossary"] = s[9:].strip()
        elif s.startswith("OUTLINE:"):
            in_outline = True
        elif in_outline and s.startswith("-"):
            out["outline"].append(s[1:].strip())
        elif s:
            in_outline = False
            body_lines.append(s)
    out["body"] = body_lines
    it = iter(tail)
    for kind, content in zip(it, it):
        c = content.strip()
        if kind == "GATES":
            try:
                out["gates"] = list(_ast.literal_eval(c))
            except (SyntaxError, ValueError):
                pass
        elif kind == "QC":
            try:
                q = _ast.literal_eval(c)
                out["qc"] = dict(q) if isinstance(q, dict) else {}
            except (SyntaxError, ValueError):
                pass
        elif kind == "NOTE":
            out["note"] = c
    if not out["body"] and not out["angle"]:
        out["body"] = [text.strip()]
    return out


def post_detail(pid: int) -> dict | None:
    conn = open_ro()
    try:
        prob = conn.execute(
            "SELECT id, statement, status FROM problems WHERE id=?", (pid,)).fetchone()
        scores = [dict(r) for r in conn.execute(
            "SELECT * FROM score_ledger WHERE pid=? ORDER BY at", (pid,))]
    finally:
        conn.close()
    if prob is None:
        return None
    manifest_row = next((m for m in (_read_json(POSTS / "manifest.json", []) or [])
                         if m.get("problem_id") == pid), {})
    manifest = {pid: manifest_row}
    txts = sorted(POSTS.glob(f"post_{pid}_v*.txt"))
    cards = sorted(POSTS.glob(f"post_{pid}_v*_card.png"))
    texts = []
    for t in txts:
        try:
            texts.append({"name": t.name, "body": t.read_text(encoding="utf-8")})
        except OSError:
            pass
    return {"problem": dict(prob), "manifest": manifest.get(pid, {}),
            "scores": scores, "texts": texts,
            "parsed": parse_post(texts[-1]["body"]) if texts else parse_post(""),
            "cards": [c.name for c in cards]}


# ------------------------------------------------------------------ quality ---
def quality_state() -> dict:
    conn = open_ro()
    try:
        ledger = [dict(r) for r in conn.execute(
            "SELECT pid, at, honesty, thesis, scenario_logic, persian, attribution, hook, total, tier FROM score_ledger ORDER BY at DESC LIMIT 30")]
    finally:
        conn.close()
    audits = sorted([p.name for p in ROOT.glob("audit_*.txt")] +
                    [p.name for p in ROOT.glob("*_report.txt")])
    return {"ledger": ledger, "audits": audits}
