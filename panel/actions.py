"""Mutating panel actions. Each mirrors a live-bot mechanism exactly:

- cancel  -> same as bot /cancel: SIGTERM the tracked worker in var/worker.pid
             (never the bot itself), clear pid/cmd files. (nexus_bot._kill_worker)
- pause   -> create var/bot_paused flag (nexus_bot.pause). resume removes it.
- endorse -> records human approval in panel state only. Final publish/sign
             stays in Telegram (needs the bot token, which the panel never holds).
- reject  -> eval/run_v03.revert_publish_marks(pid): back in queue for retry.
             NOTE: channel messages, if already delivered, must be deleted in
             Telegram; the panel cannot touch the Telegram API.

Restarting the bot itself is intentionally NOT offered: a second bot process
causes fatal Telegram session duplicates. Restart stays a Telegram /restart.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import state

ROOT = Path(__file__).resolve().parent.parent
VAR = ROOT / "var"
WORKER_PID_F = VAR / "worker.pid"
WORKER_CMD_F = VAR / "worker.cmd"
PAUSE_F = VAR / "bot_paused"
DECISIONS_F = VAR / "panel_decisions.json"
DRAFTS_F = VAR / "panel_drafts.json"


def _read_pid() -> int | None:
    try:
        return int(WORKER_PID_F.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError):
        return None


def worker_state() -> dict:
    pid = _read_pid()
    if pid is None:
        return {"pid": None, "alive": False, "cmd": None}
    try:
        os.kill(pid, 0)
        alive = True
    except OSError:
        alive = False
    cmd = None
    try:
        cmd = WORKER_CMD_F.read_text(encoding="utf-8").strip()[:200]
    except OSError:
        pass
    return {"pid": pid, "alive": alive, "cmd": cmd}


def do_cancel() -> str:
    """Mirror of nexus_bot._kill_worker."""
    pid = _read_pid()
    if pid is None:
        state.log_action("cancel", "worker", "no worker on record")
        return "no worker on record"
    try:
        os.kill(pid, 0)
    except OSError:
        for f in (WORKER_PID_F, WORKER_CMD_F):
            try:
                f.unlink()
            except OSError:
                pass
        state.log_action("cancel", "worker", "stale worker record cleared")
        return "stale worker record cleared — nothing was running"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        state.log_action("cancel", f"worker {pid}", "already gone")
        return "worker already gone"
    except OSError as e:
        state.log_action("cancel", f"worker {pid}", f"kill failed: {e}")
        return f"kill failed: {type(e).__name__}"
    for f in (WORKER_PID_F, WORKER_CMD_F):
        try:
            f.unlink()
        except OSError:
            pass
    state.log_action("cancel", f"worker {pid}", "signalled")
    return f"worker pid {pid} signalled"


# update vs propose are SEPARATE jobs (2026-09-07):
#  - run-update  = data refresh only (WDI/IMF macro series). No new problems.
#  - run-propose = news collect + extract + draft (discover.py owns its whole
#    front-end). It re-reads stored data but never refreshes macro series.
RUNNABLE = {
    "run-update": [["eval/refresh_macro.py"]],
    "run-propose": [["eval/discover.py", "--max-new", "3"]],
}

MARKERS = {
    "run-update": ["[wdi]", "[imf]", "DONE:"],
    "run-propose": ["[collect]", "[extract]", "[discover]", "READY"],
    "run-compose": ["INTEGRITY", "snapshot", "[card]", "[memory]", "reverted"],
}


def run_tracked(action: str, extra: list[str] | None = None) -> str:
    """Launch pipeline command(s) in the shared worker slot (bot-compatible).

    Mirrors nexus_bot._run_tracked's file protocol: var/worker.pid holds the
    child pid, var/worker.cmd the command line, output streams to
    var/prog_panel_<tag>.log. Refuses while any worker (bot or panel) is busy.
    The bot's /cancel kills these jobs too — one slot, one kill switch.
    """
    st = worker_state()
    if st["pid"] is not None and st["alive"]:
        return "Refused: a worker is already running — cancel it first."
    if action == "send-post":
        try:
            pid = int((extra or [""])[0])
        except ValueError:
            return "Nothing done — send needs a numeric problem id."
        post = ROOT / "posts" / f"post_{pid}_v4.txt"
        if not post.exists():
            return f"Nothing done — {post.name} does not exist."
        notified_p = VAR / "bot_notified.json"
        if notified_p.exists():
            try:
                import json as _js
                notified = _js.loads(notified_p.read_text(encoding="utf-8"))
                if str(pid) in notified:
                    return (f"Nothing done — P{pid} is already published in the "
                            f"channel. Delete it in Telegram first if you want a resend.")
            except (OSError, ValueError):
                pass
        seq = [["eval/deliver_only.py", post.name, str(pid)]]
        state.log_action("send-post", f"P{pid}", "queued via deliver_only")
    elif action == "run-compose":
        try:
            pid = int((extra or [""])[0])
        except ValueError:
            return "Nothing done — compose needs a numeric problem id."
        seq = [["eval/run_v03.py", str(pid), "--no-send"]]
    else:
        seq = [list(c) for c in RUNNABLE.get(action, [])]
    if not seq:
        return "Nothing done — unknown run request."
    tag = f"panel_{int(time.time())}"
    logp = VAR / f"prog_{tag}.log"
    logp.write_text(f"[panel] {action} queued\n", encoding="utf-8")
    flat: list[str] = []
    for c in seq:
        flat += c + ["&&"]
    flat = flat[:-1]
    script = (
        "import subprocess, sys\n"
        f"cmds = {flat!r}\n"
        "cur, code = [], 0\n"
        "for tok in cmds:\n"
        "    if tok == '&&':\n"
        "        r = subprocess.run([sys.executable] + cur)\n"
        "        code = r.returncode\n"
        "        cur = []\n"
        "        if code != 0:\n"
        "            break\n"
        "    else:\n"
        "        cur.append(tok)\n"
        "if cur:\n"
        "    code = subprocess.run([sys.executable] + cur).returncode\n"
        f"open({str(logp)!r}, 'a').write(f'\\n[panel] exit={{code}}\\n')\n"
    )
    try:
        with open(logp, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [sys.executable, "-c", script], cwd=str(ROOT),
                stdout=lf, stderr=subprocess.STDOUT)
        WORKER_PID_F.write_text(str(proc.pid), encoding="utf-8")
        WORKER_CMD_F.write_text("panel " + action, encoding="utf-8")
    except OSError as e:
        return f"Launch failed: {type(e).__name__}"
    state.log_action(action, "worker", f"launched pid {proc.pid}, log {logp.name}")
    if action == "send-post":
        return (f"Sending P{pid} to the channel now — progress under Operations. "
                f"It will appear unsigned; sign it in Telegram if you want.")
    return f"Launched {action}. Watch it under Operations — cancel works there."


def is_paused() -> bool:
    return PAUSE_F.exists()


def do_pause() -> str:
    PAUSE_F.write_text("1", encoding="utf-8")
    state.log_action("pause", "scheduler", "daily scheduler paused")
    return "Daily scheduler paused."


def do_resume() -> str:
    try:
        PAUSE_F.unlink()
    except OSError:
        pass
    state.log_action("resume", "scheduler", "daily scheduler active")
    return "Daily scheduler active."


def do_send_to_bot(pid: int, note: str = "") -> str:
    """Hand P{pid} to the NePsus bot: it delivers the draft to the admin
    chat with approve buttons. The bot picks the file up on its poll loop
    (and /drafts); delivery confirmation still comes from Telegram."""
    post = ROOT / "posts" / f"post_{pid}_v4.txt"
    if not post.exists():
        return f"Nothing done — post_{pid}_v4.txt does not exist (compose first)."
    import json as _js
    drafts = {}
    try:
        drafts = _js.loads(DRAFTS_F.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    drafts[str(pid)] = {"requested_at": _now_iso(), "note": note[:200]}
    DRAFTS_F.write_text(_js.dumps(drafts, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    state.log_action("send-to-bot", f"P{pid}", "queued for bot delivery")
    return (f"P{pid} handed to the NePsus bot. It will send the draft to "
            f"your admin chat with approve buttons (via /drafts or its "
            f"periodic check).")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def do_endorse(pid: int, note: str = "") -> str:
    state.record_decision(pid, "endorsed", note)
    return (f"P{pid} endorsed in the panel. Final publish + signature "
            f"still happen in Telegram.")


def do_reject(pid: int, reason: str = "") -> str:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "run_v03", str(ROOT / "eval" / "run_v03.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.revert_publish_marks(pid)
    _set_problem_status(pid, "archived")
    state.record_decision(pid, "rejected", reason)
    return (f"P{pid} archived — it is out of every queue. If it already "
            f"reached the channel, delete it in Telegram — the panel "
            f"cannot touch channel messages. Restore brings it back as ready.")


def _set_problem_status(pid: int, status: str) -> None:
    """Live-DB status flip (panel mutation via POST only)."""
    import sqlite3 as _sq
    conn = _sq.connect(ROOT / "nexus_think_tank.db")
    try:
        conn.execute("UPDATE problems SET status=? WHERE id=?", (status, pid))
        conn.commit()
    finally:
        conn.close()


def do_restore(pid: int, note: str = "") -> str:
    _set_problem_status(pid, "ready")
    state.record_decision(pid, "restored", note)
    return f"P{pid} restored to ready — back in the Problems list."
