"""V0.3 pipeline runner: outline-then-write with silent mechanical gates.

Usage: python eval/run_v03.py <pid>
Writes posts/post_<pid>_v4.txt (post + angle/outline + gates + QC + note).
"""
import sys
import time

sys.path.insert(0, ".")
from dotenv import load_dotenv
if not load_dotenv(".env"):
    raise SystemExit("no .env")
import os
if not os.environ.get("ROUTER_API_KEY"):
    raise SystemExit("FATAL: ROUTER_API_KEY missing")

from src.database import Database
from src.investigation_layer.reviewer import compose_v03


def hook_similarity_warn(post_fa: str, exclude_pid: int | None = None) -> float | None:
    """Max 3-gram overlap of this post's opening vs PREVIOUS ledger openings.
    Warning only -- hooks are editorial, not gated.

    BUG THIS FIXES: compose_v03 appends to the ledger before this check runs, so
    the post was compared against its OWN entry and every run reported 1.00 --
    a meaningless number that masked the real convergence between DIFFERENT
    posts. Entries for the same problem_id are now excluded."""
    import json, os, re
    lp = os.path.join("var", "editorial_ledger.json")
    if not os.path.exists(lp):
        return None
    def grams(s):
        w = re.findall(r"[\u0600-\u06FF]+", s)[:12]
        return {" ".join(w[i:i+3]) for i in range(max(0, len(w)-2))}
    g_new = grams(post_fa)
    best = 0.0
    try:
        led = json.load(open(lp, encoding="utf-8"))
        # skip this problem's own entries (incl. the one just written)
        others = [r for r in led
                  if exclude_pid is None or r.get("problem_id") != exclude_pid]
        for r in others[-20:]:
            op = r.get("opening") or ""
            g_old = grams(op)
            if g_new and g_old:
                best = max(best, len(g_new & g_old) / len(g_new | g_old))
    except Exception:
        return None
    return best


def pick_next_ready() -> int | None:
    """Lowest ready problem not yet published (per editorial ledger)."""
    import sqlite3, json, os
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row
    done = set()
    lp = os.path.join("var", "editorial_ledger.json")
    if os.path.exists(lp):
        with open(lp, encoding="utf-8") as f:
            done = {r.get("problem_id") for r in json.load(f)}
    for r in conn.execute(
            "SELECT id FROM problems WHERE status='ready' ORDER BY id"):
        if r["id"] not in done:
            conn.close()
            return r["id"]
    conn.close()
    return None


def mark_published(pid: int) -> None:
    import sqlite3
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.execute("UPDATE problems SET status='published' WHERE id=?", (pid,))
    conn.commit()
    conn.close()


def revert_publish_marks(pid: int) -> None:
    """Undo a run's publish marks so the desk retries the problem.

    ROOT CAUSE this fixes (live P25, 2026-09-03): mark_published + the ledger
    entry fired right after compose QC, BEFORE card generation and Telegram
    delivery. The run was killed in the card stage -- post composed, never
    delivered -- yet the problem was permanently 'published' and invisible to
    pick_next_ready. Publish marks now belong to confirmed delivery only; any
    run that ends without it must call this.
    Only today's ledger entries for pid are dropped (older entries may belong
    to genuinely delivered posts -- P15 legitimately appears twice).
    """
    import sqlite3, json, os
    from datetime import datetime, timezone
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.execute("UPDATE problems SET status='ready' WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    lp = os.path.join("var", "editorial_ledger.json")
    try:
        today = datetime.now(timezone.utc).date().isoformat()
        with open(lp, encoding="utf-8") as f:
            recs = json.load(f)
        kept = [r for r in recs
                if not (r.get("problem_id") == pid and r.get("date") == today)]
        if len(kept) != len(recs):
            with open(lp, "w", encoding="utf-8") as f:
                json.dump(kept, f, ensure_ascii=False, indent=1)
            print(f"  [revert] P{pid}: status->ready, "
                  f"dropped {len(recs) - len(kept)} ledger entr(ies)")
        else:
            print(f"  [revert] P{pid}: status->ready (no ledger entry to drop)")
    except Exception as e:
        print(f"  [revert] P{pid}: ledger rollback skipped: {type(e).__name__}")


def generate_card(post_path: str, pid: int) -> str | None:
    """Run the card generator as a subprocess (keeps Chrome/LLM isolated from
    the composer run). Returns (PNG path or None, layout or None) -- a visual
    is an enhancement and must never block publishing."""
    import subprocess, os, re as _re
    out_png = _re.sub(r"\.txt$", "", post_path) + "_card.png"
    try:
        p = subprocess.run([sys.executable, os.path.join("eval", "cards.py"),
                            post_path, str(pid)],
                           capture_output=True, text=True, timeout=240)
        tail = (p.stdout or "").strip().splitlines()[-3:]
        layout = None
        for line in tail:
            print("  [card] " + line)
            m = _re.search(r"layout=([a-z_]+)", line)
            if m:
                layout = m.group(1)
        if os.path.exists(out_png):
            return out_png, layout
        print("  [card] no PNG produced: " + (p.stderr or "")[-200:])
    except Exception as e:
        print(f"  [card] FAILED: {type(e).__name__}: {str(e)[:140]}")
    return None, None


def pick_visual(post_path: str, pid: int) -> tuple[str | None, str]:
    """Dual-mode visuals (user verdict 2026-09-04: number-cards aren't always
    right; some posts need a conceptual image).

    - data card (exact numbers, rendered locally) when the card pipeline found
      an aligned hero stat (any layout except no_stat);
    - concept image (text-free AI illustration) when layout is no_stat or the
      card failed -- a decorative thesis card is worse than an honest image.
    Returns (path or None, kind). Records var/visual_<pid>.json for the bot.
    """
    import json as _js
    card_png, layout = generate_card(post_path, pid)
    kind, path = "data", card_png
    if layout == "no_stat" or not card_png:
        import subprocess as _sp
        try:
            p = _sp.run([sys.executable, "eval/concept.py", post_path,
                         str(pid)], capture_output=True, text=True,
                        timeout=400)
            for line in (p.stdout or "").strip().splitlines():
                print("  [concept] " + line[-160:])
            cands = [ln.strip() for ln in (p.stdout or "").splitlines()
                     if ln.strip().endswith(".jpg")]
            if cands and os.path.exists(cands[-1]):
                kind, path = "concept", cands[-1]
        except Exception as e:
            print(f"  [concept] FAILED: {type(e).__name__}")
    try:
        with open(f"var/visual_{pid}.json", "w", encoding="utf-8") as f:
            _js.dump({"kind": kind, "path": path}, f)
    except Exception:
        pass
    print(f"  [visual] kind={kind} path={path}")
    return path, kind


def deliver_to_telegram(post_path: str, card_png: str | None,
                        tldr: str, gloss: list) -> bool:
    """Deliver via the canonical adaptive flow in send_post.py.
    Card goes as a standalone photo message; analysis follows, split with N/M
    numbering when long. Never raises -- delivery failure must not lose the post."""
    import importlib.util, os
    sender = os.path.join(os.path.expanduser("~"), "send_post.py")
    if not os.path.exists(sender):
        print("  [send] send_post.py not found -- skipped")
        return False
    try:
        spec = importlib.util.spec_from_file_location("sp", sender)
        sp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sp)
        _cap, body = sp.post_file_caption_body(post_path)
        terms = [g["term"] for g in gloss] if gloss else []
        # Telegram can time out transiently -- retry before giving up.
        import time as _t
        for attempt in range(1, 4):
            try:
                res = sp.deliver_adaptive(body, card_png=card_png, tldr=tldr,
                                          glossary_terms=terms)
                ok = all(j.get("ok") for j in res) if res else False
                print(f"  [send] {len(res)} message(s), ok={ok}")
                # Phase D: record message ids so the review bot can edit its
                # own channel messages later (approve-to-sign). Best effort.
                try:
                    import json as _js
                    ids = [j["result"]["message_id"] for j in (res or [])
                           if j.get("ok") and (j.get("result") or {}).get("message_id")]
                    if ids:
                        _lp = os.path.join("var", "last_delivery.json")
                        _prev = {}
                        if os.path.exists(_lp):
                            with open(_lp, encoding="utf-8") as _f:
                                _prev = _js.load(_f)
                        _prev[str(post_path)] = {"chat": "58638041",
                                                 "ids": ids}
                        with open(_lp, "w", encoding="utf-8") as _f:
                            _js.dump(_prev, _f, ensure_ascii=False)
                except Exception as _e:
                    print(f"  [send] id-record skipped: {type(_e).__name__}")
                return ok
            except Exception as e:
                print(f"  [send] attempt {attempt} failed: {type(e).__name__}")
                if attempt < 3:
                    _t.sleep(20)
        return False
    except Exception as e:
        print(f"  [send] FAILED: {type(e).__name__}: {str(e)[:140]}")
        return False


def record_snapshot(pid: int) -> list[str]:
    """V3 longitudinal tracking: record this post's metric readings, and return
    Persian one-liners for anything that meaningfully changed since we last
    looked at this problem. Returns [] when there is no prior snapshot.

    This is what makes the desk cumulative rather than stateless: without it,
    post 20 is no smarter than post 3 and the system can never say
    «شش ماه پیش این عدد X بود؛ امروز Y است»."""
    import importlib.util, os, sqlite3
    script = os.path.join("eval", "tracking.py")
    if not os.path.exists(script):
        return []
    try:
        spec = importlib.util.spec_from_file_location("tracking", script)
        t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(t)
        conn = sqlite3.connect("nexus_think_tank.db")
        t.init(conn)
        # changes are measured BEFORE writing the new snapshot
        deltas = t.changes(conn, pid)
        lines = t.describe_changes(deltas) if deltas else []
        t.snapshot(conn, pid)
        conn.close()
        for ln in lines:
            print("  [track] change: " + ln)
        if not lines:
            print("  [track] snapshot recorded (no prior comparison)")
        return lines
    except Exception as e:
        print(f"  [track] FAILED: {type(e).__name__}: {str(e)[:120]}")
        return []


def data_integrity_ok() -> bool:
    """Gate publishing on measurement integrity.

    A wrong-country ingestion once published Afghanistan's unemployment rate
    (13.4%) as Iran's (8.3%) in two posts. A cheap standing check is worth far
    more than a retraction, so the runner refuses to publish when OWID series
    are not Iran-only or hold no history."""
    import subprocess, os
    script = os.path.join("eval", "repair_owid.py")
    if not os.path.exists(script):
        return True
    try:
        p = subprocess.run([sys.executable, script, "verify"],
                           capture_output=True, text=True, timeout=60)
        out = (p.stdout or "").strip()
        for line in out.splitlines():
            print("  [data] " + line)
        return p.returncode == 0
    except Exception as e:
        print(f"  [data] check failed to run: {type(e).__name__} -- continuing")
        return True


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    arg = args[0] if args else "next"
    publish = "--no-send" not in sys.argv
    if not data_integrity_ok():
        print("[V0.3] ABORT: measurement integrity check failed -- "
              "run `python eval/repair_owid.py repair` first")
        return
    if arg == "next":
        pid = pick_next_ready()
        if pid is None:
            print("[V0.3] no unpublished READY problem -- run intake.py first")
            return
    else:
        pid = int(arg)
    r = None
    last_err = None
    _best_key, _best = None, None
    for attempt in range(1, 4):
        try:
            db = Database("nexus_think_tank.db")
            db.init()
            r = compose_v03(db, pid)
            db.close()
            # QC-closed loop (2026-09-05): a compose that passes gates but
            # fails QC used to ship "with note" -- the P48r2 lesson (QC
            # caught 3 run-ons + a metaphor clash, draft saved anyway).
            # Retry once more instead; the improved prompts + fresh roll
            # usually clean it. Both dirty -> last one proceeds honestly.
            _qc = (r or {}).get("qc", {})
            # BEST-OF-N (2026-09-05): fresh rolls vary wildly (P48r3: attempt
            # 1 had 2 flaws, attempt 2 had 4 worse). Track the cleanest draft
            # by (fabrications, persian errors) and deliver THAT, not the
            # last roll. Selection beats generation.
            _key = (len(_qc.get("fabrication_suspects") or []),
                    len(_qc.get("persian_errors") or []))
            # Only gate-passing drafts compete (2026-09-05 r8: the tracker
            # picked a gate-FAILED draft on its empty QC).
            if (r or {}).get("ok") and (
                    _best_key is None or _key < _best_key):
                _best_key, _best = _key, r
            if ((r or {}).get("ok") and _qc.get("qc_pass")
                    and not _qc.get("persian_errors")
                    and not _qc.get("fabrication_suspects")):
                _best = r
                try:
                    os.remove(f"var/qc_feedback_{pid}.txt")
                except Exception:
                    pass
                break
            print(f"[V0.3] P{pid} attempt {attempt}: QC unclean "
                  f"(persian={len(_qc.get('persian_errors') or [])}, "
                  f"fab={len(_qc.get('fabrication_suspects') or [])}) "
                  f"-- {'retrying' if attempt < 3 else 'proceeding with best'}")
            # Persist critic notes for the next attempt's dossier (reader in
            # compose_v03). Cleared on a clean pass below.
            try:
                with open(f"var/qc_feedback_{pid}.txt", "w",
                          encoding="utf-8") as _ff:
                    _ff.write("persian_errors:\n- " + "\n- ".join(
                        _qc.get("persian_errors") or []) +
                        "\nSEVERITY: each item above MUST be gone in the "
                        "rewrite (split the sentence / delete the phrase).")
            except Exception:
                pass
            last_err = "qc-unclean"
            if attempt < 3:
                time.sleep(45 * attempt)
                continue
            if _best is not None:
                r = _best
                print(f"[V0.3] P{pid}: delivering best draft "
                      f"fab={_best_key[0]} persian={_best_key[1]}")
            break
        except Exception as e:
            last_err = e
            print(f"[V0.3] P{pid} attempt {attempt} crashed: "
                  f"{type(e).__name__}: {str(e)[:120]}")
            time.sleep(45 * attempt)
    if r is None:
        print(f"[V0.3] P{pid} FAILED after retry: {last_err}")
        return
    if r.get("discarded"):
        print(f"[V0.3] P{pid} DISCARDED: {r.get('reason')}")
        # Demote so the desk never retries a gates-rejected problem forever
        # (the old code left it 'ready': an infinite retry loop across runs).
        revert_publish_marks(pid)  # drops this run's ledger mark
        import sqlite3 as _sq2
        _c2 = _sq2.connect("nexus_think_tank.db")
        _c2.execute("UPDATE problems SET status='candidate' WHERE id=?", (pid,))
        _c2.commit(); _c2.close()
        return
    if not r.get("ok"):
        print(f"[V0.3] P{pid} NOT OK: {r.get('reason')} | attempts={r.get('attempts')}")
        # Diagnosability (live P35: guard_ok=False with no hint which numbers
        # failed). Surface unmatched numbers + sanity violations, not just flags.
        try:
            from src.investigation_layer.reviewer import _last_gate_detail
            print(f"  gate detail: {_last_gate_detail()}")
        except Exception:
            pass
        revert_publish_marks(pid)
        return
    qc = r.get("qc", {})
    print(f"[V0.3] P{pid} OK | chars={len(r['post_fa'])} | rounds={r['rounds']}")
    print(f"  angle: {r.get('angle','')[:110]}")
    print(f"  QC pass={qc.get('qc_pass')} | signoff={str(qc.get('signoff'))[:80]}")
    print(f"  note: {r.get('reviewer_note','')[:160]}")
    tldr = r.get("tldr", "")
    gloss = r.get("glossary", [])
    cites = r.get("citations", [])
    emoji_n = sum(1 for ch in r["post_fa"] if ord(ch) > 0x1F000 or
                  ch in "🔥📊💧🌾⚡🏠👥📈📉✅❌👉🌍🌡️🚨💡")
    print(f"  tl;dr: {tldr[:100]}")
    print(f"  glossary: {[g['term'] for g in gloss]}")
    print(f"  emoji count: {emoji_n}")
    gloss_block = ""
    if gloss:
        # Two-line entries: the term on its own line, the definition under it.
        # Inline 'term: definition' mixes scripts when the term is Latin
        # (live: POWERR, PipeChina) and buries the meaning.
        gloss_block = "\n\n📚 واژه‌نامه:\n" + "\n".join(
            f"• {g['term']}\n  {g['fa']}" for g in gloss)
    if cites:
        gloss_block += "\n\n📖 منابع:\n" + "\n".join(
            f"• {c['text']}" for c in cites)
    # DATA SOURCES (user 2026-09-04: every post that reports numbers must
    # name where the numbers come from). Distinct measurement_source values
    # behind this problem's evidence -- always shown, not just papers.
    try:
        import sqlite3 as _sq3
        _c3 = _sq3.connect("nexus_think_tank.db")
        _srcs = _c3.execute(
            """SELECT DISTINCT m.measurement_source FROM measurements m
               JOIN question_evidence qe ON qe.evidence_type='measurement'
                 AND qe.evidence_id=m.id
               JOIN investigation_questions q ON q.id=qe.question_id
               WHERE q.problem_id=? AND m.measurement_source IS NOT NULL
                 AND m.measurement_source != ''""", (pid,)).fetchall()
        _c3.close()
        _src_names = sorted({r[0] for r in _srcs})
        if _src_names:
            gloss_block += ("\n\n📊 داده‌ها:\n" + "\n".join(
                f"• {s}" for s in _src_names))
    except Exception:
        pass
    out = (f"=== V0.3 ===\nANGLE: {r.get('angle','')}\n"
           f"TLDR: {tldr}\n"
           f"GLOSSARY: " + " | ".join(g['term'] for g in gloss) + "\n"
           f"OUTLINE:\n" + "\n".join(f"- {x}" for x in r.get("outline", []))
           + f"\n\n{r['post_fa']}{gloss_block}\n\n=== GATES ===\nattempts={r.get('attempts')}\n"
           f"\n=== QC ===\n{qc}\n\n=== NOTE ===\n{r.get('reviewer_note','')}\n")
    with open(f"posts/post_{pid}_v4.txt", "w", encoding="utf-8") as f:
        f.write(out)
    post_path = f"posts/post_{pid}_v4.txt"
    print("  saved " + post_path)
    sim = hook_similarity_warn(r["post_fa"], exclude_pid=pid)
    if sim is not None:
        flag = "  [warn]" if sim > 0.35 else "  hook"
        print(f"{flag} similarity {sim:.2f} vs other posts' openings "
              f"(strategy={r.get('hook_strategy', '?')})")
    try:
        from src.investigation_layer.writer import news_framing_score, unexplained_claims
        nf = news_framing_score(r["post_fa"])
        if nf >= 2:
            print(f"  [warn] opening still uses news framing (score={nf})")
        else:
            print(f"  hook news-framing score={nf} (ok)")
        ux = unexplained_claims(r["post_fa"])
        if ux:
            print(f"  [warn] {len(ux)} unexplained causal claim(s) remain")
            for u in ux[:2]:
                print(f"      - {u[:110]}")
        else:
            print("  explanation gate: no unexplained causal claims (ok)")
        # P3 claim-level audit: which sentences have evidence-id backing?
        try:
            import sqlite3 as _sq3
            from eval.claim_check import verify_claims
            from src.investigation_layer.dossier import (build_dossier,
                                                         format_dossier_for_composer)
            class _DB:
                def _require_connection(self):
                    c = _sq3.connect("nexus_think_tank.db")
                    c.row_factory = _sq3.Row

                    class _W:
                        def __init__(self, c): self.c = c
                        def execute(self, *a, **k): return self.c.execute(*a, **k)
                    return _W(c)
            _dtxt = format_dossier_for_composer(build_dossier(_DB(), pid))
            cr = verify_claims(r["post_fa"], _dtxt)
            print(f"  claim backing: {cr['fully_backed']}/{cr['n_numbered_sentences']} "
                  f"numbered sentences fully evidence-backed")
            if cr["partially_backed"]:
                for s in cr["sentences"]:
                    if not s["fully_backed"]:
                        print(f"    [unbacked] {s['sentence'][:90]}")
        except Exception as _ce:
            print(f"  claim audit skipped: {type(_ce).__name__}")
    except Exception:
        pass

    # ---- V3: longitudinal tracking (record readings, detect real change) ----
    record_snapshot(pid)

    # ---- A4: remember which indicators this post presented, so future posts
    # are penalized for reusing the same headline numbers (anti-generic desk).
    try:
        import sys as _s5, os as _o5, sqlite3 as _sq5
        _ep = _o5.path.join(_o5.getcwd(), "eval")
        if _ep not in _s5.path:
            _s5.path.insert(0, _ep)
        from evidence_memory import record_headline, snapshot_indicators
        _c5 = _sq5.connect("nexus_think_tank.db")
        record_headline(pid, snapshot_indicators(_c5, pid))
        _c5.close()
        print("  [memory] headline indicators recorded")
    except Exception as _e5:
        print(f"  [memory] skipped: {type(_e5).__name__}")

    # ---- publish stage: card -> adaptive Telegram delivery -----------------
    # Publish marks belong to CONFIRMED delivery only (live P25: the old code
    # marked published before this stage, so a kill here lost the post while
    # hiding it from the desk forever).
    card_png, _vkind = pick_visual(post_path, pid)
    if publish:
        ok = deliver_to_telegram(post_path, card_png, tldr, gloss)
        if ok:
            mark_published(pid)
            print(f"  [send] P{pid} delivered + marked published")
        else:
            revert_publish_marks(pid)
            print(f"  [send] P{pid} DELIVERY FAILED -- marks reverted, desk will retry")
    else:
        revert_publish_marks(pid)
        print("  [send] skipped (--no-send; compose marks reverted)")


if __name__ == "__main__":
    main()
