"""Phase D: Nexus review bot (@NePsusbot) — approval + custom questions.

Runs ONLY while the user's PC is on (by design). Polling via
python-telegram-bot v22 behind the xray proxy; long pipeline work runs in
worker threads as subprocesses (a compose crash must never kill polling).

v1 commands (single admin only):
  /start            bootstrap admin on first use
  /status           queue (ready/candidate) + recent deliveries
  /ask <text>       custom question -> gates -> draft (NEVER auto-publishes)
  /pause /resume    stop/start the daily desk job
  /compose <pid>    (re-)compose a problem, send draft for approval
  /update           refresh database (macro + news)
  /propose          new problems with Compose buttons
  /cancel           kill running work (handlers are non-blocking)
  /restart          restart the bot process
Post notices carry [تأیید + امضا] [حذف] buttons:
  approve -> appends SIGNATURE to the channel message (edit, own message)
  delete  -> deletes the channel messages + reverts publish marks

Setup: 1) make @NePsusbot admin of the channel. 2) /start in DM.
Run: python eval/nexus_bot.py  (or eval/start_bot.bat)
"""
import asyncio
import glob
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

for ln in open("%LOCALAPPDATA%/hermes/.env".replace("%LOCALAPPDATA%",
               os.environ.get("LOCALAPPDATA", "")), encoding="utf-8",
               errors="replace"):
    ln = ln.strip()
    if ln and not ln.startswith("#") and "=" in ln:
        k, _, v = ln.partition("=")
        os.environ.setdefault(k.strip(), v.strip())

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,  # noqa: E402
                          ContextTypes, MessageHandler, filters)

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s",
                    level=logging.INFO)
# Hygiene: httpx logs full request URLs (which embed the bot token).
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("nexus_bot")

TOKEN = os.environ.get("NEXUS_BOT_TOKEN", "")
PROXY = "http://127.0.0.1:10809"
CHAT = "58638041"  # publication channel (same id the desk delivers to)
SIGNATURE = "Pragman\n@Masire_Nov"
VAR = "var"
ADMIN_F = os.path.join(VAR, "bot_admin.json")
NOTIFIED_F = os.path.join(VAR, "bot_notified.json")
DRAFTS_F = os.path.join(VAR, "panel_drafts.json")
PAUSE_F = os.path.join(VAR, "bot_paused")
DELIVERY_F = os.path.join(VAR, "last_delivery.json")


def _load_panel_drafts() -> dict:
    """Pending panel handoffs: {pid: {requested_at, note, done?}}."""
    return _load_json(DRAFTS_F, {})


def _mark_draft_done(pid: str) -> None:
    d = _load_panel_drafts()
    if str(pid) in d:
        d[str(pid)]["done"] = True
        d[str(pid)]["done_at"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                                time.gmtime())
        _save_json(DRAFTS_F, d)


async def _deliver_pending_drafts(context) -> int:
    """Send every requested-but-not-done draft to the admin. Returns count."""
    if not await _ensure_admin(context):
        return 0
    drafts = _load_panel_drafts()
    sent = 0
    for pid_s, meta in sorted(drafts.items()):
        if meta.get("done"):
            continue
        if not os.path.exists(f"posts/post_{pid_s}_v4.txt"):
            continue
        try:
            chat_id = await _admin_chat(context)
            await context.bot.send_message(
                chat_id, f"Panel handed me P{pid_s} — sending the draft.")
            await _send_draft(int(pid_s), context, int(pid_s), ask=False)
            _mark_draft_done(pid_s)
            sent += 1
        except Exception as e:
            print(f"[drafts] P{pid_s} failed: {type(e).__name__}: {str(e)[:120]}")
    return sent


async def _admin_chat(context) -> int:
    aid = _admin()
    if aid:
        return aid
    raise RuntimeError("no admin registered yet (/start once)")


async def _ensure_admin(context) -> bool:
    return bool(_admin())


def _load_json(p, default):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


def _admin() -> int | None:
    return _load_json(ADMIN_F, {}).get("admin_id")


def _run(cmd: list[str], timeout: int = 590) -> tuple[int, str]:
    p = subprocess.run([sys.executable] + cmd, capture_output=True,
                       text=True, timeout=timeout)
    tail = (p.stdout or "")[-1200:] + ((p.stderr or "")[-400:] if p.stderr else "")
    return p.returncode, tail


def _problems(filter_status: str | None = None) -> list[dict]:
    import sqlite3
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row
    q = "SELECT id, status, source, substr(statement,1,90) s FROM problems"
    if filter_status:
        q += f" WHERE status='{filter_status}'"
    q += " ORDER BY id"
    out = [dict(r) for r in conn.execute(q)]
    conn.close()
    return out


async def _is_admin(update: Update) -> bool:
    a = _admin()
    uid = update.effective_user.id if update.effective_user else None
    return bool(a and uid == a)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from telegram import ReplyKeyboardMarkup
    uid = update.effective_user.id
    if not os.path.exists(ADMIN_F):
        _save_json(ADMIN_F, {"admin_id": uid})
        log.info("admin bootstrapped: %s", uid)
    elif uid != _admin():
        return  # strangers: silence (allowlist, no reply)
    kb = ReplyKeyboardMarkup(
        [["📊 Status", "🆕 Latest posts"],
         ["❓ New question", "⏸ Pause / ▶️ Resume"]],
        resize_keyboard=True)
    ready = _problems("ready")
    await update.message.reply_text(
        "Nexus desk 🗞\n"
        f"Ready problems: {len(ready)} | see recent deliveries via Latest posts.\n"
        "Ask with /ask (e.g. /ask How high is monthly inflation?).\n"
        "News posts auto-publish unsigned; your approval signs them.",
        reply_markup=kb)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        return
    ready = _problems("ready")
    cand = _problems("candidate")
    lines = [f"Ready: {len(ready)} | Candidates: {len(cand)}"]
    for r in ready[:6]:
        lines.append(f"• P{r['id']} [{r['source'] or 'news'}] {r['s']}")
    paused = os.path.exists(PAUSE_F)
    lines.append("Scheduler: " + ("paused" if paused else "active"))
    await update.message.reply_text("\n".join(lines)[:3500])


async def latest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Resend the most recent published posts as reviewable drafts."""
    if not await _is_admin(update):
        return
    import re
    try:
        n = max(1, min(5, int((context.args or ["3"])[0])))
    except ValueError:
        n = 3
    files = sorted(glob.glob("posts/post_*_v4.txt"),
                   key=os.path.getmtime, reverse=True)[:n]
    if not files:
        await update.message.reply_text("No posts.")
        return
    await update.message.reply_text(f"📚 {len(files)} recent posts:")
    for f in files:
        m = re.search(r"post_(\d+)_v4", f)
        if m:
            try:
                await _send_draft(update.effective_chat.id, context,
                                  int(m.group(1)), ask=False)
            except Exception as e:
                await update.message.reply_text(
                    f"P{m.group(1)} failed to send: {type(e).__name__}")


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Persian reply-keyboard buttons -> same actions as commands."""
    if not await _is_admin(update):
        return
    t = (update.message.text or "").strip()
    if t.startswith("📊"):
        await status(update, context)
    elif t.startswith("🆕"):
        await latest(update, context)
    elif t.startswith("❓"):
        await update.message.reply_text(
            "Send your question with /ask.\nExample: /ask How has the war affected online businesses?")
    elif "Pause" in t or "Resume" in t:
        if os.path.exists(PAUSE_F):
            await resume(update, context)
        else:
            await pause(update, context)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("handler error: %s", context.error)
    try:
        if _admin():
            await context.bot.send_message(
                _admin(), f"⚠️ Bot error: {type(context.error).__name__} — "
                          f"bot stays alive and continues.")
    except Exception:
        pass


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        return
    await update.message.reply_text(
        "Nexus desk help 🗞\n\n"
        "📊 Status — ready/candidate problems, scheduler state\n"
        "🆕 Latest posts — 3 recent posts with approve/delete\n"
        "❓ New question — ask with /ask, e.g.:\n"
        "   /ask How high is monthly inflation?\n"
        "   The question passes the same gates: if evidence is thin, "
        "it tells you exactly what's missing instead of fabricating.\n"
        "🖥 Pipeline (stepwise) — /update (refresh database) → /propose\n"
        "   (new problems, tap Compose on the one you want) →\n"
        "   /compose <id> (compose + send draft for approval)\n"
    "   /drafts (deliver posts handed over from the panel)\n"
        "⛔ /cancel stops running work, 🔄 /restart restarts me\n"
        "⏸ Pause / ▶️ Resume — daily scheduler\n"
        "/help — this help")


def _bar(frac: float, width: int = 10) -> str:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "▓" * n + "░" * (width - n) + f" {int(frac * 100)}%"


WORKER_PID_F = os.path.join(VAR, "worker.pid")
WORKER_CMD_F = os.path.join(VAR, "worker.cmd")


def _worker_alive() -> int | None:
    """Pid of the tracked worker if actually running, else None (and the
    stale pid file is cleared so a dead worker never blocks new work)."""
    try:
        pid = int(open(WORKER_PID_F).read().strip())
    except Exception:
        return None
    try:
        os.kill(pid, 0)
        return pid
    except Exception:
        for f in (WORKER_PID_F, WORKER_CMD_F):
            try:
                os.remove(f)
            except Exception:
                pass
        return None


def _kill_worker() -> str:
    """Kill any running tracked worker. Returns what happened."""
    import signal
    pid = _worker_alive()
    if pid is None:
        try:
            open(WORKER_PID_F).read()
            return "stale worker record cleared -- nothing was running"
        except Exception:
            return "no worker on record"
    try:
        os.kill(pid, signal.SIGTERM)
        for f in (WORKER_PID_F, WORKER_CMD_F):
            try:
                os.remove(f)
            except Exception:
                pass
        return f"worker pid {pid} signalled"
    except ProcessLookupError:
        return "worker already gone"
    except Exception as e:
        return f"kill failed: {type(e).__name__}"


async def _run_tracked(context, chat_id: int, status_msg, cmd: list[str],
                       markers: list[str], timeout: int = 600):
    """Run a pipeline command as a KILLABLE subprocess with live progress.

    The child pid goes to var/worker.pid so /cancel can stop it (threads
    cannot be killed -- the v5 'frozen bot' lesson). Progress shows the last
    log line + elapsed so a stall is informative, never a frozen bar.
    TimeoutExpired kills the child and reports partial output honestly.
    Returns (exit_code, full_output).

    Concurrency guard: only one tracked worker at a time (single pid slot).
    A second command while one runs is refused -- /cancel first. Handlers
    are non-blocking (block=False) so /cancel is never stuck behind work.
    """
    import time as _t
    busy = _worker_alive()
    if busy is not None:
        try:
            what = open(WORKER_CMD_F).read().strip()[:60]
        except Exception:
            what = "a job"
        await status_msg.edit_text(
            f"Busy (pid {busy}: {what}). /cancel it first.")
        return busy, ""
    tag = f"{int(_t.time())}"
    logp = f"var/prog_{tag}.log"
    open(logp, "w").close()
    t0 = _t.time()
    proc = await asyncio.get_running_loop().run_in_executor(
        None, lambda: subprocess.Popen(
            [sys.executable] + cmd,
            stdout=open(logp, "w", encoding="utf-8", errors="replace"),
            stderr=subprocess.STDOUT))
    try:
        with open(WORKER_PID_F, "w") as f:
            f.write(str(proc.pid))
        with open(WORKER_CMD_F, "w") as f:
            f.write(" ".join(cmd))
    except Exception:
        pass
    code, out = None, ""
    try:
        while proc.poll() is None:
            if _t.time() - t0 > timeout:
                proc.kill()
                out = open(logp, encoding="utf-8", errors="replace").read()
                try:
                    await status_msg.edit_text(
                        f"{_bar(0.0)}\n⏱ timeout after {timeout}s -- partial log:\n{out[-800:]}")
                except Exception:
                    pass
                return 124, out
            await asyncio.sleep(20)
            try:
                out = open(logp, encoding="utf-8", errors="replace").read()
            except Exception:
                out = ""
            hit = sum(1 for m in markers if m in out)
            frac = hit / max(1, len(markers))
            last = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
            el = int(_t.time() - t0)
            try:
                await status_msg.edit_text(
                    f"{_bar(frac)}\n{last[0][:120]}\n{el}s elapsed")
            except Exception:
                pass
        code = proc.returncode
        out = open(logp, encoding="utf-8", errors="replace").read()
        try:
            await status_msg.edit_text(f"{_bar(1.0)}\ndone.")
        except Exception:
            pass
        return code, out
    finally:
        for f in (WORKER_PID_F, WORKER_CMD_F):
            try:
                os.remove(f)
            except Exception:
                pass


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop whatever the bot is running."""
    if not await _is_admin(update):
        return
    await update.message.reply_text(f"⛔ {_kill_worker()}")


async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Stop work and restart the bot process itself."""
    if not await _is_admin(update):
        return
    _kill_worker()
    await update.message.reply_text("🔄 Restarting bot…")
    await asyncio.sleep(2)
    os.execv(sys.executable, [sys.executable] + sys.argv)


async def update_db(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: pull fresh data and update the database (no posts)."""
    if not await _is_admin(update):
        return
    msg = await update.message.reply_text("Step 1/3: updating database…")
    code1, out1 = await _run_tracked(
        context, update.effective_chat.id, msg,
        ["eval/refresh_macro.py"],
        ["[wdi]", "[imf]", "DONE:"], timeout=900)
    import re as _re4
    m = _re4.search(r"DONE: wdi_new=(\d+).*?imf_new=(\d+)", out1)
    macro = f"+{m.group(1)} WDI rows, +{m.group(2)} IMF rows" if m else "macro refresh done"
    msg2 = await update.message.reply_text("Fetching news into the DB (dry, no problems)…")
    code2, out2 = await _run_tracked(
        context, update.effective_chat.id, msg2,
        ["eval/discover.py", "--dry"],
        ["[Gate 0]", "[Gate 1]", "[extract]", "[discover]"], timeout=900)
    g = _re4.search(r"\[Gate 1\] Done: (\d+) new", out2)
    news = f"+{g.group(1)} articles" if g else "news ingest done"
    await update.message.reply_text(
        f"✅ Database updated: {macro}; {news}.\nNext: /propose")


async def propose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: propose new problems/questions for selection (no compose)."""
    if not await _is_admin(update):
        return
    msg = await update.message.reply_text("Step 2/3: hunting new problems…")
    code, out = await _run_tracked(
        context, update.effective_chat.id, msg,
        ["eval/discover.py", "--max-new", "3"],
        ["[Gate 0]", "[Gate 1]", "[extract]", "[discover]", "READY"],
        timeout=900)
    import sqlite3
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row
    newp = [dict(r) for r in conn.execute(
        """SELECT id, status, substr(statement,1,120) s FROM problems
           WHERE status IN ('ready','candidate') AND id >=
             (SELECT MAX(id)-6 FROM problems) ORDER BY id DESC LIMIT 5""")]
    conn.close()
    if not newp:
        await update.message.reply_text("No new problems this round.")
        return
    for r in newp:
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Compose this one ✍️",
                                 callback_data=f"select_{r['id']}")]])
        await context.bot.send_message(
            update.effective_chat.id,
            f"P{r['id']} [{r['status']}]\n{r['s']}", reply_markup=kb)


async def compose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Compose one ready problem without sending: /compose 29"""
    if not await _is_admin(update):
        return
    try:
        pid = int((context.args or [""])[0])
    except ValueError:
        await update.message.reply_text("Example: /compose 29")
        return
    await update.message.reply_text(f"Composing P{pid} (no send)…")
    prog = await update.message.reply_text("▓░░░░░░░░░ 0%")
    code, tail = await _run_tracked(
        context, update.effective_chat.id, prog,
        ["eval/run_v03.py", str(pid), "--no-send"],
        ["INTEGRITY", "snapshot", "[card]", "[memory]", "reverted"], timeout=900)
    await update.message.reply_text(f"P{pid} exit={code}\n{tail[-1500:]}")
    if code == 0:
        await _send_draft(update.effective_chat.id, context, pid, ask=False)


async def pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        return
    open(PAUSE_F, "w").write("1")
    await update.message.reply_text("Daily scheduler paused.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        return
    if os.path.exists(PAUSE_F):
        os.remove(PAUSE_F)
    await update.message.reply_text("Daily scheduler active.")


def _draft_buttons(pid: int, ask: bool = False):
    tag = "ask" if ask else "pub"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Approve + sign ✅", callback_data=f"approve_{tag}_{pid}"),
        InlineKeyboardButton("Delete 🗑", callback_data=f"delete_{tag}_{pid}"),
    ]])


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _is_admin(update):
        return
    text = " ".join(context.args or []).strip()
    if not text:
        await update.message.reply_text("Example: /ask How high is monthly inflation?")
        return
    await update.message.reply_text("Question received; checking gates…")
    prog = await update.message.reply_text("⏳ Starting…")
    # Killable subprocess (2026-09-04 freeze: the old thread-based _run with a
    # 1500s timeout could never be cancelled and showed no real progress --
    # same disease as the v5 /update freeze. Tracked Popen fixes both).
    code, tail = await _run_tracked(
        context, update.effective_chat.id, prog,
        ["eval/ask.py", text, "--compose"],
        markers=["ASK: questions", "ASK: gather", "ASK: substrates",
                 "ASK: P", "READY", "NOT OK", "FAILED"],
        timeout=1500)
    # find THIS run's problem id (parsed from output -- max-id picked a stale
    # post on gate failure and sent an irrelevant draft: the v2 bug).
    import re as _re3
    _m = _re3.search(r"ASK: P(\d+) READY", tail)
    pid = int(_m.group(1)) if _m else None
    if code != 0 or pid is None:
        await update.message.reply_text(f"No post.\n{tail[-1500:]}")
        return
    if "READY" not in tail:
        await update.message.reply_text(f"P{pid} stayed candidate:\n{tail[-1500:]}")
        return
    await _send_draft(update.effective_chat.id, context, pid, ask=True)


async def _send_draft(chat_id: int, context, pid: int, ask: bool):
    """Send composed card + post text to the admin with approve buttons."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sp", os.path.expanduser("~/send_post.py"))
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    post_path = f"posts/post_{pid}_v4.txt"
    # Dual-mode visual: sidecar from the publish run, else data card fallback.
    visual = _load_json(f"var/visual_{pid}.json", {})
    card = visual.get("path") or post_path.replace(".txt", "_card.png")
    kind = visual.get("kind", "data")
    _cap, body = sp.post_file_caption_body(post_path)
    parts = sp.split_with_numbering(body, 3800)
    header = ("🖼 concept image (illustrative, not data)" if kind == "concept"
              else "📊 data card")
    if os.path.exists(card):
        with open(card, "rb") as f:
            await context.bot.send_photo(chat_id, f, caption=header)
    # Per-part delivery with retry (live P33: photo arrived, single text part
    # threw once, user got an orphan card and no text -- one failed part must
    # never sink the whole draft).
    import asyncio as _aio
    for i, part in enumerate(parts):
        for attempt in range(3):
            try:
                await context.bot.send_message(chat_id, part)
                break
            except Exception as e:
                if attempt == 2:
                    await context.bot.send_message(
                        chat_id, f"⚠️ Part {i+1}/{len(parts)} failed to send "
                        f"({type(e).__name__}); the rest is in the channel/file.")
                else:
                    await _aio.sleep(5)
    await context.bot.send_message(chat_id, f"Draft P{pid} — not published.",
                                   reply_markup=_draft_buttons(pid, ask))


async def _approve_publish(context, pid: int) -> str:
    """Publish a draft (ask) or sign an auto-published post. Returns report."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sp", os.path.expanduser("~/send_post.py"))
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    post_path = f"posts/post_{pid}_v4.txt"
    notified = _load_json(NOTIFIED_F, {})
    if str(pid) not in notified:
        # ask-draft: publish bare now, then sign
        loop = asyncio.get_running_loop()
        code, tail = await loop.run_in_executor(
            None, lambda: _run(["eval/deliver_only.py", post_path, str(pid)]))
        if code != 0:
            return f"انتشار ناموفق:\n{tail[-800:]}"
    _cap, body = sp.post_file_caption_body(post_path)
    parts = sp.split_with_numbering(body, 3800)
    delivery = _load_json(DELIVERY_F, {}).get(post_path, {})
    ids = delivery.get("ids", [])
    if not ids:
        return "Channel message id not found (last_delivery)."
    last_id = ids[-1]
    new_text = parts[-1] + "\n\n" + SIGNATURE
    try:
        await context.bot.edit_message_text(new_text, chat_id=CHAT,
                                            message_id=last_id)
    except Exception as e:
        return f"Sign failed: {type(e).__name__} {str(e)[:120]}"
    notified[str(pid)] = int(time.time())
    _save_json(NOTIFIED_F, notified)
    return f"P{pid} approved and signed. ✅"


async def _delete_published(context, pid: int) -> str:
    from eval.run_v03 import revert_publish_marks
    post_path = f"posts/post_{pid}_v4.txt"
    delivery = _load_json(DELIVERY_F, {}).get(post_path, {})
    n = 0
    for mid in delivery.get("ids", []):
        try:
            await context.bot.delete_message(CHAT, mid)
            n += 1
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, revert_publish_marks, pid)
    return f"P{pid}: {n} message(s) deleted, back in queue."


async def _send_draft_after_compose(update, context, pid: int):
    """Step 3 worker: compose with progress bar, then send the draft."""
    prog = await context.bot.send_message(update.effective_chat.id,
                                          "▓░░░░░░░░░ 0%")
    code, tail = await _run_tracked(
        context, update.effective_chat.id, prog,
        ["eval/run_v03.py", str(pid), "--no-send"],
        ["INTEGRITY", "snapshot", "[card]", "[memory]", "reverted"], timeout=900)
    await context.bot.send_message(update.effective_chat.id,
                                   f"P{pid} exit={code}\n{tail[-1500:]}")
    if code == 0:
        await _send_draft(update.effective_chat.id, context, pid, ask=False)


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not await _is_admin(update):
        return
    try:
        parts = q.data.split("_")
        if parts[0] == "select" and len(parts) == 2:
            # Step 3: user picked a proposed problem -> compose it.
            await q.edit_message_reply_markup(None)
            pid = int(parts[1])
            await context.bot.send_message(
                update.effective_chat.id, f"Step 3/3: composing P{pid}…")
            await _send_draft_after_compose(update, context, pid)
            return
        action, _tag, pid = parts
        pid = int(pid)
    except ValueError:
        return
    if action == "approve":
        rep = await _approve_publish(context, pid)
    else:
        rep = await _delete_published(context, pid)
    await q.edit_message_reply_markup(None)
    await context.bot.send_message(update.effective_chat.id, rep)


async def _daily(context: ContextTypes.DEFAULT_TYPE):
    if os.path.exists(PAUSE_F) or not _admin():
        return
    admin = _admin()
    # Tracked (2026-09-04): the old thread-based _run was invisible and
    # unkillable by /cancel. Same Popen machinery as commands.
    try:
        prog = await context.bot.send_message(admin, "🌙 Nightly desk running…")
    except Exception:
        return
    code, tail = await _run_tracked(
        context, admin, prog,
        ["eval/desk.py", "--discover", "--n", "2"],
        ["[Gate 0]", "[Gate 1]", "[extract]", "[discover]", "READY"],
        timeout=1500)
    try:
        await prog.edit_text(f"🌙 Nightly desk done (exit={code}).")
    except Exception:
        pass
    # notify drafts for fresh post files
    notified = _load_json(NOTIFIED_F, {})
    fresh = []
    for f in glob.glob("posts/post_*_v4.txt"):
        m = os.path.getmtime(f)
        if time.time() - m > 20 * 3600:
            continue
        import re
        mm = re.search(r"post_(\d+)_v4", f)
        if mm and mm.group(1) not in notified:
            fresh.append(int(mm.group(1)))
    for pid in sorted(fresh):
        try:
            await _send_draft(admin, context, pid, ask=False)
            notified[str(pid)] = int(time.time())
        except Exception as e:
            log.warning("notify P%s failed: %s", pid, e)
    _save_json(NOTIFIED_F, notified)
    await context.bot.send_message(
        admin, f"Daily report: exit={code}\n{tail[-1500:]}")


async def _drafts_poll(context) -> None:
    """job_queue callback: deliver panel-handed drafts to the admin chat."""
    try:
        n = await _deliver_pending_drafts(context)
        if n:
            print(f"[drafts] delivered {n} panel draft(s)")
    except Exception as e:
        print(f"[drafts] poll failed: {type(e).__name__}: {str(e)[:120]}")


async def drafts_cmd(update, context) -> None:
    """/drafts — deliver everything the panel handed over, right now."""
    if not await _is_admin(update):
        return
    n = await _deliver_pending_drafts(context)
    if n == 0:
        pending = [k for k, v in _load_panel_drafts().items() if not v.get("done")]
        msg = "Nothing pending from the panel."
        if pending:
            msg = f"Pending {len(pending)} but post file(s) missing: {', '.join('P'+p for p in pending)}"
        await update.message.reply_text(msg)


def main():
    if not TOKEN:
        raise SystemExit("NEXUS_BOT_TOKEN missing from hermes .env")
    # Single-instance guard: two live bots double-send cards and drafts
    # (the likely source of 'random' duplicates). Stale pids are harmless:
    # liveness is verified, not just file existence. NOTE: `ps -p` cannot
    # see native Windows processes from git-bash, so it ALWAYS reported
    # 'dead' and duplicates started anyway (2026-09-04 double-start).
    # os.kill(pid, 0) is the portable liveness probe (PermissionError =
    # alive but unowned; ProcessLookupError = dead).
    _lock = os.path.join(VAR, "bot.pid")
    try:
        _old = int(open(_lock).read().strip())
        try:
            os.kill(_old, 0)
            raise SystemExit(f"another bot instance is alive (pid {_old})")
        except ProcessLookupError:
            pass  # stale pid -- safe to take over
        except SystemExit:
            raise
        except PermissionError:
            raise SystemExit(f"another bot instance is alive (pid {_old})")
    except SystemExit:
        raise
    except Exception:
        pass
    with open(_lock, "w") as f:
        f.write(str(os.getpid()))
    app = (Application.builder().token(TOKEN).proxy(PROXY)
           .get_updates_proxy(PROXY).build())
    # block=False everywhere: with default blocking handlers, a long /ask or
    # /compose queues /cancel behind it -- the exact "cancel does nothing"
    # complaint (2026-09-04). Non-blocking + the _run_tracked busy guard.
    app.add_handler(CommandHandler("start", start, block=False))
    app.add_handler(CommandHandler("status", status, block=False))
    app.add_handler(CommandHandler("ask", ask, block=False))
    app.add_handler(CommandHandler("pause", pause, block=False))
    app.add_handler(CommandHandler("resume", resume, block=False))
    app.add_handler(CommandHandler("latest", latest, block=False))
    app.add_handler(CommandHandler("help", help_cmd, block=False))
    app.add_handler(CommandHandler("compose", compose, block=False))
    app.add_handler(CommandHandler("update", update_db, block=False))
    app.add_handler(CommandHandler("propose", propose, block=False))
    app.add_handler(CommandHandler("cancel", cancel, block=False))
    app.add_handler(CommandHandler("restart", restart, block=False))
    app.add_handler(CommandHandler("drafts", drafts_cmd, block=False))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_daily(_daily, time=__import__("datetime").time(9, 0))
        # Panel handshake poll: deliver drafts the panel handed over.
        app.job_queue.run_repeating(_drafts_poll, interval=60, first=30)

    # Self-maintained command menu (2026-09-04): the menu lives in code, not
    # in a manual API call, so pruning a command here prunes it in Telegram
    # on the next /restart. English descriptions (no ????? mojibake).
    async def _set_menu(_app):
        from telegram import BotCommand
        try:
            await _app.bot.set_my_commands([
                BotCommand("start", "Start the bot"),
                BotCommand("status", "Queue and scheduler state"),
                BotCommand("update", "Step 1: refresh database"),
                BotCommand("propose", "Step 2: new post proposals"),
                BotCommand("compose", "Step 3: compose a proposal"),
                BotCommand("ask", "Ask a custom question"),
                BotCommand("latest", "Recent posts"),
                BotCommand("pause", "Pause daily scheduler"),
                BotCommand("resume", "Resume daily scheduler"),
                BotCommand("cancel", "Stop running work NOW"),
                BotCommand("restart", "Restart the bot"),
                BotCommand("help", "Help"),
            ])
        except Exception as e:
            log.warning("menu update failed: %s", type(e).__name__)
    app.post_init = _set_menu
    log.info("nexus bot polling (proxy %s)", PROXY)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
