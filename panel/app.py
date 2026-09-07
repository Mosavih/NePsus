"""NePsus panel — read-first control room over the live Nexus system. stdlib only.

Run:  python panel/app.py            -> http://localhost:8001
Test: python -m pytest panel/tests/ -q   (from repo root)

GET pages are read-only. Mutations happen only via POST /actions after a
GET /confirm step. See actions.py for what each action really does.
"""
from __future__ import annotations

import mimetypes
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import llm
import read
import re
import templates
from actions import (do_cancel, do_endorse, do_pause, do_reject, do_restore,
                     do_resume, do_send_to_bot, is_paused, run_tracked,
                     worker_state)
from read import (channels, funnel_counts, job_progress, latest_scores,
                  post_detail, posts_queue, problems_queue, quality_state,
                  sources_status, update_state)
from state import get_decisions, recent_actions

templates._TIER_PROVIDER = lambda: llm.effective_models()

HERE = Path(__file__).resolve().parent
POST_IMG_RE = re.compile(r"^post_\d+_v\d+(_card)?\.(png|jpg|txt)$")

ACTIONS = ("cancel", "pause", "resume", "endorse", "reject", "restore",
           "run-update", "run-propose", "run-compose",
           "send-post", "send-to-bot", "probe-tier", "set-stage",
           "route-disable", "route-enable")


def problems_body(show_archived: bool = False) -> str:
    """Single renderer for /problems and its ?show=archived view."""
    return templates.problems_page(problems_queue(), posts_queue(),
                                    get_decisions(), show_archived)


def page(path: str, msg: str = "") -> tuple[int, str, str]:
    if path == "/":
        body = templates.dashboard_page(funnel_counts(), update_state())
        return 200, templates.base("Dashboard", path, body, msg), "text/html"
    if path == "/update":
        body, refresh = templates.update_page(channels(), update_state(), job_progress())
        return 200, templates.base("Operations", path, body, msg, refresh), "text/html"
    if path in ("/posts", "/problems"):
        # One list: every proposal with its composed post (if any) inline.
        # /posts stays as an alias so old links keep working.
        return 200, templates.base("Problems", "/problems",
                                    problems_body(False), msg), "text/html"
    if path == "/pipeline":
        body = templates.pipeline_page(funnel_counts())
        return 200, templates.base("Pipeline health", path, body, msg), "text/html"
    if path == "/jobs":
        body = templates.jobs_page(update_state(), worker_state(), is_paused(),
                                   recent_actions())
        return 200, templates.base("Bot", path, body, msg), "text/html"
    if path == "/quality":
        body = templates.quality_page(quality_state(), latest_scores(),
                                       posts_queue())
        return 200, templates.base("Quality", path, body, msg), "text/html"
    if path == "/post":
        return 200, templates.base("Post", path, "", msg), "text/html"
    if path == "/routes":
        body, _ = templates.routes_page(llm.effective_models(),
                                        llm.fallback_chain(), llm.route_statuses(),
                                        llm.health_ledger())
        return 200, templates.base("LLM routes", path, body, msg), "text/html"
    if path == "/confirm":
        return 200, templates.base("Confirm", path, "", msg), "text/html"
    return 404, templates.base("Not found", path, "<h1>Not found</h1>", msg), "text/html"


class Handler(BaseHTTPRequestHandler):
    server_version = "NePsusPanel/0.2"

    def log_message(self, *args):  # quieter logs
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/static/"):
            target = (HERE / parsed.path.lstrip("/")).resolve()
            if not str(target).startswith(str(HERE)) or not target.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        qs = parse_qs(parsed.query)
        msg = qs.get("msg", [""])[0][:300]
        if parsed.path == "/routes/edit":
            tier = qs.get("tier", [""])[0]
            self._send(200, templates.base(
                "Edit route", parsed.path,
                templates.route_edit_page(llm.effective_models(), tier), ""))
            return
        if parsed.path == "/post":
            try:
                try:
                    pid = int(qs.get("pid", ["0"])[0])
                except ValueError:
                    pid = 0
                d = post_detail(pid) if pid > 0 else None
                if d is None:
                    self._send(404, templates.base("Post", "/post",
                                                   "<h1>No such problem</h1>", ""))
                    return
                self._send(200, templates.base(
                    f"Problem {pid}", "/post",
                    templates.post_page(d, get_decisions()), ""))
            except Exception as exc:
                print(f"panel post failed: {exc!r}")
                self._send(500, templates.base("Post", "/post",
                                               "<h1>Panel error</h1><p>Check the console.</p>", ""))
            return
        if parsed.path.startswith("/post-img/"):
            name = parsed.path.rsplit("/", 1)[-1]
            if not POST_IMG_RE.match(name):
                self.send_error(404)
                return
            target = (HERE.parent / "posts" / name).resolve()
            if not str(target).startswith(str(HERE.parent / "posts")) or not target.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path == "/problems":
            show_archived = qs.get("show", [""])[0] == "archived"
            self._send(200, templates.base(
                "Problems", "/problems",
                problems_body(show_archived), msg))
            return
        if parsed.path == "/confirm":
            try:
                action = qs.get("action", [""])[0]
                pid = qs.get("pid", [""])[0]
                if action not in ACTIONS:
                    self._send(400, templates.base("Confirm", "/confirm",
                                                   "<h1>Unknown action</h1>", ""))
                    return
                query = {"tier": qs.get("tier", [""])[0],
                         "route": qs.get("route", [""])[0]}
                self._send(200, templates.base("Confirm", "/confirm",
                                               templates.confirm_page(action, pid, query), ""))
            except Exception as exc:
                print(f"panel confirm failed: {exc!r}")
                self._send(500, templates.base("Confirm", "/confirm",
                                               "<h1>Panel error</h1><p>Check the console.</p>", ""))
            return
        try:
            code, body, ctype = page(parsed.path, msg)
        except Exception as exc:  # never leak tracebacks to the browser
            code, body, ctype = 500, templates.base(
                "Error", parsed.path, "<h1>Panel error</h1><p>Check the console.</p>", ""), "text/html"
            print(f"panel error on {parsed.path}: {exc!r}")
        self._send(code, body, ctype)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/actions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        action = form.get("action", [""])[0]
        back = form.get("back", ["/"])[0]
        if back not in ("/", "/update", "/problems", "/pipeline", "/jobs", "/quality"):
            back = "/"
        try:
            pid = int(form.get("pid", ["0"])[0] or 0)
        except ValueError:
            pid = 0
        note = form.get("note", [""])[0][:500]
        try:
            if action == "cancel":
                result = do_cancel()
            elif action == "pause":
                result = do_pause()
            elif action == "resume":
                result = do_resume()
            elif action == "endorse" and pid > 0:
                result = do_endorse(pid, note)
            elif action == "reject" and pid > 0:
                result = do_reject(pid, note)
            elif action == "restore" and pid > 0:
                result = do_restore(pid, note)
            elif action in ("run-update", "run-propose"):
                result = run_tracked(action)
            elif action == "run-compose" and pid > 0:
                result = run_tracked(action, [str(pid)])
                back = "/jobs"
            elif action == "send-post" and pid > 0:
                result = run_tracked("send-post", [str(pid)])
                back = "/problems"
            elif action == "send-to-bot" and pid > 0:
                result = do_send_to_bot(pid, note)
                back = "/problems"
            elif action == "set-stage":
                task = form.get("task", [""])[0]
                combo = form.get("combo", [""])[0]
                result = llm.set_stage_combo(task, combo)
            elif action == "probe-tier":
                tier = form.get("tier", [""])[0]
                res = llm.probe_tier(tier)
                result = ("Route answered in %d ms — healthy." % res["latency_ms"]
                          if res.get("ok") else f"Route FAILING: {res.get('error')}")
                back = "/routes"
            elif action in ("route-disable", "route-enable"):
                route = form.get("route", [""])[0].strip()
                if not route:
                    result = "Nothing done — no route given."
                else:
                    result = llm.set_route_enabled(
                        route, enabled=(action == "route-enable"), hours=12.0)
            else:
                result = "Nothing done — unknown or incomplete request."
        except Exception as exc:
            result = f"Action failed safely: {type(exc).__name__}"
            print(f"panel action {action} failed: {exc!r}")
        from urllib.parse import quote
        self.send_response(303)
        self.send_header("Location", f"{back}?msg={quote(result[:300])}")
        self.end_headers()

    def _send(self, code: int, body: str, ctype: str = "text/html"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _acquire_instance_lock(port: int) -> bool:
    """Windows-safe single-instance check: bind-test the port first (the
    honest test), then record our pid. A stale pid file alone must not
    block startup (os.kill(pid, 0) lies across MSYS/WinPTY sessions)."""
    import socket
    test = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        test.bind(("127.0.0.1", port))
    except OSError:
        print(f"panel already running on :{port} (bind test failed) — exiting.")
        return False
    finally:
        test.close()
    (HERE / "panel.pid").write_text(str(os.getpid()), encoding="utf-8")
    return True


def main(port: int = 8001):
    if not _acquire_instance_lock(port):
        raise SystemExit(1)
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"NePsus panel on http://localhost:{port} (pid {os.getpid()})")
    try:
        server.serve_forever()
    finally:
        pf = HERE / "panel.pid"
        if pf.exists():
            try:
                pf.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
