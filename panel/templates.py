"""Server-rendered HTML builders. English UI. Status = color + icon + text, never color-only."""
from __future__ import annotations

import html

from src.investigation_layer.models import COMBOS

NAV = [("/", "Dashboard"), ("/update", "Operations"), ("/problems", "Problems"),
       ("/pipeline", "Pipeline"), ("/quality", "Ratings"),
       ("/routes", "LLM routes"), ("/jobs", "Bot")]

STATUS_META = {
    "fresh": ("●", "ok", "Fresh"),
    "stale": ("▲", "warn", "Stale"),
    "missing": ("■", "bad", "Missing"),
    "live": ("●", "ok", "Live"),
    "idle": ("○", "muted", "Idle"),
    "pass": ("✔", "ok", "PASS"),
    "revise": ("✎", "warn", "REVISE"),
    "done": ("✔", "ok", "Done"),
    "open": ("○", "warn", "Open"),
}


def badge(kind: str, text: str = "") -> str:
    icon, cls, default = STATUS_META.get(kind, ("•", "muted", kind))
    label = text or default
    return f'<span class="badge {cls}"><span class="ic" aria-hidden="true">{icon}</span>{html.escape(label)}</span>'


def base(title: str, path: str, body: str, msg: str = "", refresh: bool = False) -> str:
    links = "".join(
        f'<a href="{u}" class="{"active" if u == path else ""}">{label}</a>'
        for u, label in NAV)
    banner = f'<p class="banner">{html.escape(msg)}</p>' if msg else ""
    meta = '<meta http-equiv="refresh" content="15">' if refresh else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
{meta}
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — NePsus panel</title>
<link rel="stylesheet" href="/static/style.css">
</head>
<body>
<header class="topbar"><div class="brand">◈ NePsus panel</div><nav>{links}</nav></header>
<main>{banner}{body}</main>
<script src="/static/app.js"></script>
</body>
</html>"""


def _log_block(l: dict, is_open: bool = False) -> str:
    name = html.escape(l["file"])
    tail = html.escape("\n".join(l["tail"]))
    flag = " open" if is_open else ""
    return f"<details{flag}><summary>{name}</summary><pre>{tail}</pre></details>"


def _tier_badge(r: dict) -> str:
    try:
        tier = int(r.get("tier"))
    except (TypeError, ValueError):
        tier = 0
    if tier >= 2:
        return badge("pass")
    return badge("revise", "tier " + str(r.get("tier")))


_TIER_PROVIDER = None  # set by app.py: lambda: llm.effective_models()


def llm_tiers() -> list[dict]:
    return _TIER_PROVIDER() if _TIER_PROVIDER else []


def _rel_time(ts) -> str:
    if not ts:
        return "never"
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
        if age < 1:
            return f"{int(age * 60)} min ago"
        if age < 48:
            return f"{int(age)} h ago"
        return f"{int(age / 24)} d ago"
    except ValueError:
        return str(ts)


def dashboard_page(funnel: dict, upd: dict) -> str:
    cards = "".join(
        f'<div class="card"><div class="num">{funnel.get(k, 0)}</div><div class="lbl">{label}</div></div>'
        for k, label in [("problems", "Problems"), ("questions", "Questions"),
                         ("findings", "Findings"), ("measurements", "Measurements"),
                         ("events", "Events"), ("artifacts", "Artifacts")])
    bot = badge("live", "Bot answering") if upd["bot_alive"] else badge("idle", "Bot quiet")
    chart = bars([(str(s or "—"), n) for s, n in funnel.get("problems_by_status", [])])
    return (f"<h1>Campaign overview</h1>"
            f'<section class="grid">{cards}</section>'
            f'<section><h2>Problems by status</h2>{chart}</section>'
            f'<section class="row"><h2>Bot</h2>{bot}</section>'
            f'<section><h2>Latest delivery</h2><p>{html.escape(upd["delivery"])}</p></section>')


def _progress_bar(frac: float, label: str) -> str:
    pct = max(0.0, min(1.0, frac)) * 100
    filled = int(pct // 10)
    return (f"<div class='progress' role='progressbar' aria-label='{html.escape(label)}'>"
            f"<span class='p-fill' style='width:{pct:.0f}%'></span>"
            f"<span class='p-txt'>{'▓' * filled}{'░' * (10 - filled)} {pct:.0f}% — {html.escape(label)}</span></div>")


def update_page(chans: list[dict], upd: dict, prog: dict | None = None) -> str:
    prog = prog or {}
    if prog.get("running"):
        ops = (_progress_bar(prog["frac"], f"{prog['action']} · step {prog['step']} · {prog['elapsed']}s")
               + f"<p dir='ltr'>{html.escape(prog['last_line'])}</p>"
               + f"<p><a class='btn danger' href='/confirm?action=cancel'>⛔ Cancel this run</a> "
               + f"<span class='dim'>log: {html.escape(prog['log'])}</span></p>")
        refresh = True
    else:
        ops = (f"<p>No job running. Start one — same commands as the bot, cancelable here.</p>"
               f"<p><a class='btn' href='/confirm?action=run-update'>⟳ Database update</a> "
               f"<a class='btn' href='/confirm?action=run-propose'>◉ Propose problems</a></p>"
               f"<form method='get' action='/confirm' class='row'>"
               f"<input type='hidden' name='action' value='run-compose'>"
               f"<label>Compose problem <input type='text' name='pid' size='4' placeholder='id'></label> "
               f"<button class='btn' type='submit'>Continue →</button></form>")
        refresh = False
    cards = "".join(
        f"<div class='card wide'><div class='lbl'>{html.escape(c['name'])}</div>"
        f"<div>{html.escape(c['detail'])}</div>"
        f"<div class='dim'>{html.escape(c['role'])}</div>"
        f"<div class='fresh'>✔ {html.escape(c['freshness'])}</div></div>"
        for c in chans)
    logs = "".join(_log_block(l) for l in upd["recent_logs"])
    bot = badge("live", "Bot answering") if upd["bot_alive"] else badge("idle", "Bot quiet")
    return (f"<h1>Operations</h1>"
            f"<section><h2>Pipeline runs</h2>{ops}</section>"
            f"<section><h2>Evidence channels</h2><div class='grid'>{cards}</div></section>"
            f"<section class='row'><h2>Bot</h2>{bot}<span class='dim'>{html.escape(upd['ask'])}</span></section>"
            f"<section><h2>Delivery</h2><p>{html.escape(upd['delivery'])}</p></section>"
            f"<details><summary>Recent run logs (machine detail)</summary>{logs or '<p class=dim>none</p>'}</details>",
            refresh)


def _score_badge(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return badge("open", "unscored")
    if s >= 8:
        return badge("pass", f"{s}")
    return badge("revise", f"{s}")


def _short_model(model) -> str:
    if not model:
        return "model ?"
    return str(model).split("/")[-1]


def problems_page(problems, posts=None, decisions=None, show_archived=False):
    """ONE list: every proposal newest-first, with its composed post inline.

    Row per problem: statement + status; if a post was composed, its score,
    versions, card state and Send/Endorse actions ride along; otherwise a
    Compose action. Archived rows hide unless show_archived.
    """
    decisions = decisions or {}
    by_pid = {m.get("problem_id"): m for m in (posts or [])}
    open_rows, arch_rows = [], []
    for it in (problems or []):
        pid = it.get("id")
        mine = decisions.get(pid, {})
        archived = (it.get("status") == "archived") or mine.get("decision") == "rejected"
        st = (badge("revise", "archived") if archived
              else badge("open", str(it.get("status", ""))))
        dec = (f" {badge('revise', mine['decision'] + ' in panel')}"
               if mine and mine.get("decision") not in ("restored",) else "")
        src = (f" <span class='dim'>{html.escape(str(it.get('source', '')))}</span>"
               if it.get("source") else "")
        stmt = html.escape(str(it.get("statement", ""))[:160])
        post = by_pid.get(pid) or {}
        if post:
            mdec = (post.get("decision") or "").upper()
            pst = (badge("pass") if mdec == "PASS" else
                   badge("revise") if mdec == "REVISE" else badge("open", "no decision"))
            card = "card" if post.get("card") else "no card"
            info = (f"{_short_model(post.get('model'))} · "
                    f"{_score_badge(post.get('score'))} · v{post.get('versions', 0)} · "
                    f"{html.escape(post.get('latest_txt') or 'no text')} · {card}")
            bot_state = ("sent to bot" if post.get("sent_to_bot")
                         else "compose done")
            extra = (f"<br>{pst} <span class='dim'>{info} · {bot_state}</span>"
                     f"<br><span class='acts'><a href='/post?pid={pid}'>Open</a> · "
                     f"<a href='/confirm?action=send-to-bot&amp;pid={pid}'>Send to bot</a> · "
                     f"<a href='/confirm?action=send-post&amp;pid={pid}'>Send to channel</a> · "
                     f"<a href='/confirm?action=endorse&amp;pid={pid}'>Endorse</a></span>")
        else:
            extra = "<br><span class='dim'>no post composed yet</span>"
        if archived:
            arch_rows.append(
                f"<li><div><strong>Problem {pid}</strong>{src} "
                f"<span class='dim'>{stmt}</span><br>{st}{dec}{extra}<br>"
                f"<span class='acts'><a href='/confirm?action=restore&amp;pid={pid}'>Restore</a>"
                f"</span></div></li>")
        else:
            open_rows.append(
                f"<li><div><strong>Problem {pid}</strong>{src} "
                f"<span class='dim'>{stmt}</span><br>{st}{dec}{extra}<br>"
                f"<span class='acts'><a href='/confirm?action=run-compose&amp;pid={pid}'>Compose</a> · "
                f"<a href='/confirm?action=reject&amp;pid={pid}'>Reject</a></span></div></li>")
    if show_archived:
        arch_html = ''.join(arch_rows) or "<li><span class='dim'>nothing archived</span></li>"
        body = (f"<p class='dim'>Archived problems \u2014 rejected in the panel. "
                f"<a href='/problems'>back to open problems</a>.</p>"
                f"<ul class='airy'>{arch_html}</ul>")
    else:
        empty = "<li><span class='dim'>no problems yet \u2014 run Propose problems under Operations</span></li>"
        body = (f"<p class='dim'>{len(open_rows)} open problem(s), newest first. "
                f"Composed posts ride inline with Send/Endorse; Endorse stays panel-only, "
                f"final publish + signature happen in Telegram. Rejected problems disappear \u2014 "
                f"<a href='/problems?show=archived'>archived ({len(arch_rows)})</a>.</p>"
                f"<ul class='airy'>{''.join(open_rows) or empty}</ul>")
    return f"<h1>Problems</h1>{body}"



def post_page(d: dict, decisions: dict | None = None) -> str:
    decisions = decisions or {}
    pid = d["problem"]["id"]
    p = d["parsed"]
    head = (f"<h1>Problem {pid}</h1>"
            f"<p>{html.escape(str(d['problem'].get('statement', '')))}</p>"
            f"<p>{badge('open', str(d['problem'].get('status', '')))} "
            f"{badge('pass') if (d['manifest'].get('decision') or '').upper() == 'PASS' else badge('revise', str(d['manifest'].get('decision', 'no manifest decision')))}</p>")
    article = ""
    if p["angle"]:
        article += f"<p class='hook' dir='auto'>{html.escape(p['angle'])}</p>"
    if p["tldr"]:
        article += f"<p class='tldr' dir='auto'><strong>خلاصه — </strong>{html.escape(p['tldr'])}</p>"
    for para in p["body"]:
        article += f"<p dir='auto'>{html.escape(para)}</p>"
    if p["glossary"]:
        article += f"<p class='dim' dir='auto'>واژه‌نامه: {html.escape(p['glossary'])}</p>"
    plan = ""
    if p["outline"]:
        items = "".join(f"<li dir='auto'>{html.escape(o)}</li>" for o in p["outline"])
        plan = (f"<details><summary>Story plan ({len(p['outline'])} beats)</summary>"
                f"<ol class='plan'>{items}</ol></details>")
    qc_html = ""
    qc = p["qc"]
    if qc:
        checks = "".join(
            badge("pass", label) if qc.get(key) is True
            else badge("revise", label) if qc.get(key) is False
            else ""
            for key, label in [("qc_pass", "QC pass"), ("hook_ok", "Hook"),
                              ("length_ok", "Length")])
        suspects = qc.get("fabrication_suspects") or []
        sus = (f"<p>⚠ needs a human eye: {html.escape(', '.join(map(str, suspects)))}</p>"
               if suspects else "")
        sign = f"<p dir='auto'>{html.escape(str(qc.get('signoff', '')))}</p>" if qc.get("signoff") else ""
        qc_html = (f"<section><h2>Quality check</h2><p>{checks}</p>{sus}{sign}</section>")
    note_html = (f"<section><h2>Reviewer note</h2><p dir='auto'>{html.escape(p['note'])}</p></section>"
                 if p["note"] else "")
    gates_html = ""
    if p["gates"]:
        rows = "".join(
            f"<li><span>{'✔' if g.get('guard_ok') else '✎'} "
            f"{html.escape(str(g.get('revision') and 'revision' or 'draft'))}</span> "
            f"<span class='dim'>sanity {g.get('sanity_violations', '?')}, "
            f"unexplained {g.get('unexplained', '?')}</span></li>"
            for g in p["gates"] if isinstance(g, dict))
        gates_html = (f"<details><summary>Technical gate history ({len(p['gates'])})</summary>"
                      f"<ul class='airy'>{rows}</ul></details>")
    old = ""
    if len(d["texts"]) > 1:
        old = "".join(
            f"<details><summary>{html.escape(t['name'])}</summary>"
            f"<pre dir='auto'>{html.escape(t['body'])}</pre></details>"
            for t in d["texts"][:-1])
        old = f"<details><summary>Older versions ({len(d['texts']) - 1})</summary>{old}</details>"
    cards = "".join(
        f"<figure><img src='/post-img/{html.escape(c)}' alt='card {html.escape(c)}' loading='lazy'>"
        f"<figcaption>{html.escape(c)}</figcaption></figure>"
        for c in d["cards"]) or "<p class='dim'>no card yet</p>"
    mine = decisions.get(pid)
    dec_line = f"<p>{badge('pass' if mine['decision'] == 'endorsed' else 'revise', mine['decision'] + ' in panel')}</p>" if mine else ""
    acts = (f"<p><a class='btn' href='/confirm?action=endorse&amp;pid={pid}'>Endorse ✔</a> "
            f"<a class='btn danger' href='/confirm?action=reject&amp;pid={pid}'>Reject ✖</a></p>")
    rated = []
    for s in d["scores"]:
        crit = " · ".join(f"{k} {s.get(k)}" for k in
                          ("honesty", "thesis", "scenario_logic", "persian", "attribution", "hook"))
        rated.append(
            f"<li><div><strong>{html.escape(str(s.get('at', '')))}</strong> "
            f"<span class='dim'>{html.escape(str(s.get('tier', '')))}</span></div>"
            f"<div>{_score_badge(s.get('total'))}</div></li>"
            f"<li class='sub'><span class='dim'>{html.escape(crit)}</span><br>"
            f"<span class='dim'>notes: {html.escape(str(s.get('notes', '')))}</span></li>")
    rated_html = (f"<section><h2>Model ratings ({len(d['scores'])})</h2>"
                  f"<ul class='airy'>{''.join(rated)}</ul></section>" if rated else "")
    return (f"{head}{dec_line}{acts}"
            f"<section class='article'><h2>Post · {html.escape(p['version'])}</h2>{article}</section>"
            f"{rated_html}{plan}{qc_html}{note_html}{gates_html}"
            f"<section><h2>Card</h2>{cards}</section>"
            f"{old}")


def routes_page(tiers: list[dict], chain: list[str], statuses: dict,
                health: dict | None = None):
    """Stage -> combo assignment grid + combo health + quarantine controls."""
    health = health or {}
    from datetime import datetime as _dt, timezone as _tz

    def _quarantine_badge(route: str) -> str:
        e = health.get(route)
        if not e:
            return ""
        until = e.get("disabled_until")
        fails = e.get("fails", 0)
        bits = []
        if until:
            try:
                mins = max(0, int((_dt.fromisoformat(until)
                                   - _dt.now(_tz.utc)).total_seconds() / 60))
                bits.append(badge("bad", f"quarantined {mins} min more"))
            except ValueError:
                pass
        elif fails:
            bits.append(badge("warn", f"{fails} recent failure(s)"))
        if e.get("last_ok"):
            bits.append(f"<span class='dim'>last OK {html.escape(str(e['last_ok'])[11:16])} UTC</span>")
        if e.get("error"):
            bits.append(f"<span class='dim'>{html.escape(str(e['error'])[:90])}</span>")
        if e.get("note"):
            bits.append(f"<span class='dim'>({html.escape(e['note'])})</span>")
        return " ".join(bits)

    def _row_actions(route: str) -> str:
        e = health.get(route) or {}
        if e.get("disabled_until"):
            return f"<a class='btn' href='/confirm?action=route-enable&amp;route={html.escape(route)}'>Re-enable</a>"
        return f"<a class='btn danger' href='/confirm?action=route-disable&amp;route={html.escape(route)}'>Kill 12 h</a>"

    stage_rows = []
    for t in tiers:
        cur = t["current"]
        st = statuses.get(t["key"])
        if st:
            probe = (badge("pass", f"answered {st['latency_ms']} ms")
                     if st["ok"] else badge("bad", "failing"))
        else:
            probe = badge("open", "not probed yet")
        qb = _quarantine_badge(cur)
        src = "overridden in .env" if t["overridden"] else "pipeline default"
        sel = "".join(
            f"<option value='{c}'{' selected' if c == cur else ''}>{c}</option>"
            for c in COMBOS)
        stage_rows.append(
            f"<li><div><strong>{html.escape(t['task'])}</strong> "
            f"<span class='dim'>{html.escape(t['role'])}</span><br>"
            f"{probe} {qb}<br>"
            f"<form method='post' action='/actions' class='row'>"
            f"<input type='hidden' name='action' value='set-stage'>"
            f"<input type='hidden' name='task' value='{t['key']}'>"
            f"<select name='combo'>{sel}</select> "
            f"<button class='btn' type='submit'>Assign</button></form>"
            f"<span class='dim'>{src} · env {t['env']}</span></div>"
            f"<div>{_row_actions(cur)}</div></li>")
    combo_rows = []
    for c in COMBOS:
        qb = _quarantine_badge(c)
        combo_rows.append(
            f"<li><div><strong>{html.escape(c)}</strong></div>"
            f"<div>{qb} <a class='btn' href='/confirm?action=probe-tier&amp;tier={c}'>Probe</a> {_row_actions(c)}</div></li>")
    legacy = [m for m in dict.fromkeys(list(health.keys()) + list(chain))
              if m not in COMBOS]
    legacy_rows = "".join(
        f"<li><div><code>{html.escape(m)}</code></div>"
        f"<div>{_quarantine_badge(m)} {_row_actions(m)}</div></li>"
        for m in legacy) or "<li><span class='dim'>none</span></li>"
    chain_html = " → ".join(f"<code>{html.escape(m)}</code>" for m in chain)
    page = (f"<h1>LLM routes</h1>"
            f"<p class='dim'>Each pipeline stage is assigned one of your four router "
            f"combos (Mechanical / Complicated / MostComplicated / Medium.Supporter). "
            f"Inside a combo the router walks its providers; if a whole combo stops "
            f"answering, the pipeline falls to the next combo below. Kill quarantines "
            f"a combo everywhere for 12 h — no more grinding.</p>"
            f"<section><h2>Stage assignments</h2><ul class='airy'>{''.join(stage_rows)}</ul></section>"
            f"<section><h2>Combo health</h2><ul class='airy'>{''.join(combo_rows)}</ul></section>"
            f"<section><h2>Emergency order when a combo dies</h2><p>{chain_html}</p></section>"
            f"<section><h2>Legacy routes (seen by older code)</h2><ul class='airy'>{legacy_rows}</ul></section>")
    return page, tiers


def route_edit_page(tiers: list[dict], tier_key: str) -> str:
    t = next((x for x in tiers if x["key"] == tier_key), None)
    if t is None:
        return ("<h1>Unknown stage</h1><p><a class='btn' href='/routes'>Back</a></p>")
    opts = "".join(f"<option value='{c}'{' selected' if c == t['current'] else ''}>{c}</option>"
                   for c in COMBOS)
    return (f"<h1>Change “{html.escape(t['task'])}” combo</h1>"
            f"<p class='dim'>{html.escape(t['role'])}. Currently "
            f"<strong>{html.escape(t['current'])}</strong>.</p>"
            f"<form method='post' action='/actions'>"
            f"<input type='hidden' name='action' value='set-stage'>"
            f"<input type='hidden' name='task' value='{t['key']}'>"
            f"<label>Combo<br><select name='combo'>{opts}</select></label><br><br>"
            f"<button class='btn' type='submit'>Save to .env</button> "
            f"<a class='btn' href='/routes'>Cancel</a></form>"
            f"<p class='dim'>Saved into .env with a backup in var/. The next pipeline "
            f"run picks it up; running jobs keep their current combo.</p>")


def bars(rows: list[tuple[str, float]], unit: str = "") -> str:
    top = max([v for _, v in rows] + [1])
    out = []
    for label, value in rows:
        pct = 100 * (value / top) if top else 0
        out.append(
            f"<div class='bar-row'><span class='bar-lbl'>{html.escape(label)}</span>"
            f"<span class='bar-track'><span class='bar-fill' style='width:{pct:.1f}%'></span></span>"
            f"<span class='bar-val'>{value:g}{html.escape(unit)}</span></div>")
    return "".join(out)


def pipeline_page(funnel: dict) -> str:
    segs = "".join(
        f"<li><div><strong>{html.escape(str(s or '—'))}</strong></div><div>{n} problems</div></li>"
        for s, n in funnel.get("problems_by_status", []))
    return (f"<h1>Pipeline health</h1>"
            f"<section><h2>Problems by status</h2><ul class='airy'>{segs}</ul></section>"
            f"<section><h2>Funnel</h2>{bars([('Questions', funnel['questions']), ('Open questions', funnel['questions_open']), ('Studies', funnel['studies']), ('Findings', funnel['findings']), ('Measurements', funnel['measurements']), ('Claims', funnel['claims']), ('Events', funnel['events'])])}</section>")


def jobs_page(upd: dict, worker: dict | None = None, paused: bool = False,
                history: list[dict] | None = None) -> str:
    bot = badge("live", "Bot answering") if upd["bot_alive"] else badge("idle", "Bot quiet")
    sched = badge("warn", "Scheduler PAUSED") if paused else badge("live", "Scheduler running")
    toggle = "<a class='btn' href='/confirm?action=resume'>Resume</a>" if paused else "<a class='btn' href='/confirm?action=pause'>Pause daily</a>"
    hist = "".join(
        f"<li><span>{html.escape(h.get('at', ''))} — {html.escape(h.get('action', ''))}"
        f" {html.escape(h.get('target', ''))}</span> "
        f"<span class='dim'>{html.escape(h.get('detail', ''))}</span></li>"
        for h in (history or [])) or "<li><span class='dim'>nothing yet</span></li>"
    return (f"<h1>Bot</h1><section class='row'><h2>This channel</h2>{bot}</section>"
            f"<p class='dim'>Pipeline runs live on the Operations page now — one place to start, watch, and cancel.</p>"
            f"<section class='row'><h2>Scheduler</h2>{sched} {toggle}</section>"
            f"<section><h2>What the panel has done</h2><ul class='airy'>{hist}</ul></section>"
            f"<p class='dim'>Restarting the bot itself stays a Telegram /restart — a second bot process would break the channel session.</p>")


EXPLAIN = {
    "send-post": ("Send post to the channel",
                  "Runs the pipeline's own deliver_only.py for this post: card + text go to the Telegram channel exactly like a bot delivery. It appears unsigned; approve it in Telegram to sign."),
    "send-to-bot": ("Hand post to the NePsus bot",
                    "Queues this post for the bot: it sends the draft to your admin chat with approve buttons, and approval publishes + signs in the channel. This is the normal path; Send to channel bypasses the bot."),
    "cancel": ("Cancel running worker",
               "Sends SIGTERM to the tracked worker PID in var/worker.pid and clears the slot — exactly what Telegram /cancel does. The bot itself keeps running."),
    "pause": ("Pause daily scheduler",
              "Creates var/bot_paused, which the bot checks before scheduled runs — same as Telegram /pause."),
    "resume": ("Resume daily scheduler",
               "Removes var/bot_paused — same as Telegram /resume."),
    "endorse": ("Endorse post",
                "Records your approval in the panel only. Final publish + signature still happen in Telegram (the panel never holds the bot token)."),
    "reject": ("Reject + archive",
               "Runs the pipeline's own revert_publish_marks: then archives the problem: it leaves every queue. Channel messages already delivered must still be deleted in Telegram. Restore from the archived list."),
    "restore": ("Restore problem",
                "Sets the problem back to ready -- it reappears in the Problems list. Panel decision recorded."),
    "run-update": ("Run database update",
                   "Macro-data refresh ONLY (WDI/IMF series) — the same two commands as Telegram /update. Runs in the shared worker slot with a live log; cancelable."),
    "run-propose": ("Propose new problems",
                    "News collect + extract + draft, up to 3 new problems — the same command as Telegram /propose. Runs in the shared worker slot with a live log; cancelable."),
    "run-compose": ("Compose problem",
                    "Composes one problem without sending — the same command as Telegram /compose. Runs in the shared worker slot with a live log; cancelable."),
    "probe-tier": ("Probe this route",
                   "Sends a tiny test message through the router to check the route answers. Costs one small LLM call. The verdict is shared with the pipeline's health ledger."),
    "route-disable": ("Quarantine this route",
                      "Writes it into the shared health ledger: every pipeline stage and the panel skip it for 12 h (or until you re-enable). Use this the moment a route starts 429-grinding."),
    "route-enable": ("Re-enable this route",
                     "Clears the quarantine. The next call tests it like any other route."),
}


def confirm_page(action: str, pid: str = "", query=None) -> str:
    query = query or {}
    title, what = EXPLAIN.get(action, ("Unknown", ""))
    if action == "probe-tier":
        # The Probe button names a combo directly; honor it instead of
        # asking again. No selection -> offer the four combos.
        pre = query.get("tier", "")
        if pre in COMBOS:
            opts = f"<option value='{pre}' selected>{pre}</option>"
            named = f"<p>Route: <strong><code>{html.escape(pre)}</code></strong></p>"
        else:
            opts = "".join(f"<option value='{c}'>{c}</option>" for c in COMBOS)
            named = ""
        return (f"<h1>{html.escape(title)}</h1><p>{html.escape(what)}</p>{named}"
                f"<form method='post' action='/actions'>"
                f"<input type='hidden' name='action' value='probe-tier'>"
                f"<label>Route<br><select name='tier'>{opts}</select></label><br><br>"
                f"<button class='btn danger' type='submit'>Probe now</button> "
                f"<a class='btn' href='/routes'>Back</a></form>")
    if action in ("route-disable", "route-enable"):
        route = query.get("route", "") or pid
        if not route:
            return "<h1>Missing route</h1><p><a class='btn' href='/routes'>Back</a></p>"
        verb = "quarantine" if action == "route-disable" else "re-enable"
        return (f"<h1>{title}</h1><p>{what}</p>"
                f"<p>Route: <strong><code>{html.escape(route)}</code></strong></p>"
                f"<form method='post' action='/actions'>"
                f"<input type='hidden' name='action' value='{action}'>"
                f"<input type='hidden' name='route' value='{html.escape(route)}'>"
                f"<button class='btn danger' type='submit'>Confirm {verb}</button> "
                f"<a class='btn' href='/routes'>Back</a></form>")
    try:
        p = int(pid)
    except ValueError:
        p = 0
    if action in ("endorse", "reject", "send-post") and p <= 0:
        return "<h1>Missing problem id</h1>"
    back = "/problems" if action in ("endorse", "reject", "send-post", "send-to-bot", "restore", "run-compose") else "/jobs"
    pid_row = f"<p>Problem: <strong>P{p}</strong></p>" if p else ""
    note_row = ""
    if action in ("endorse", "reject", "restore"):
        note_row = ("<label>Note (optional)<br><input type='text' name='note' maxlength='500' size='60'></label><br><br>")
    return (f"<h1>{html.escape(title)}</h1>{pid_row}<p>{html.escape(what)}</p>"
            f"<form method='post' action='/actions'>"
            f"<input type='hidden' name='action' value='{html.escape(action)}'>"
            f"<input type='hidden' name='pid' value='{p}'>"
            f"<input type='hidden' name='back' value='{back}'>"
            f"{note_row}<button class='btn danger' type='submit'>Confirm</button> "
            f"<a class='btn' href='{back}'>Back</a></form>")


def quality_page(q: dict, scores: dict | None = None,
                 posts: list | None = None) -> str:
    """Ratings page: EVERY composed post (scored or not) with its rubric
    scores and model — the human-facing view of posts + score_ledger."""
    scores = scores or {}
    posts = posts or []
    seen, rows = set(), []
    for m in posts:
        pid = m.get("problem_id")
        if pid in seen:
            continue
        seen.add(pid)
        sc = scores.get(pid, {})
        model = sc.get("model") or m.get("model") or "?"
        delivered = "delivered" if m.get("delivered") else (
            "sent to bot" if m.get("sent_to_bot") else "composed")
        rows.append(
            f"<li><div><strong><a href='/post?pid={pid}'>Problem {pid}</a></strong> "
            f"<span class='dim'>{delivered} · {_short_model(model)} · "
            f"{html.escape(m.get('latest_txt') or '')}</span></div>"
            f"<div>{_score_badge(sc.get('total'))} "
            f"<a class='acts' href='/post?pid={pid}'>open</a></div></li>")
    for r in q["ledger"]:
        pid = r["pid"]
        if pid in seen:
            continue
        seen.add(pid)
        model = r.get("tier") or "?"
        rows.append(
            f"<li><div><strong><a href='/post?pid={pid}'>Problem {pid}</a></strong> "
            f"<span class='dim'>no post file · generated/evaluated by {_short_model(model)}</span></div>"
            f"<div>{_score_badge(r.get('total'))} "
            f"<a class='acts' href='/post?pid={pid}'>open</a></div></li>")
    if not rows:
        rows.append("<li><span class='dim'>no composed posts yet</span></li>")
    hist: dict[str, int] = {}
    for pid, s in scores.items():
        if pid == "_rounds" or s.get("total") is None:
            continue
        try:
            t = float(s["total"])
        except (TypeError, ValueError):
            continue
        bucket = "8–10 (publish-ready)" if t >= 8 else f"{int(t)}–{int(t) + 1}"
        hist[bucket] = hist.get(bucket, 0) + 1
    order = ["8–10 (publish-ready)", "7–8", "6–7", "5–6", "4–5", "3–4", "2–3", "1–2", "0–1"]
    chart = bars([(b, hist[b]) for b in order if b in hist], " posts") if hist \
        else "<p class='dim'>no scores yet</p>"
    by_model: dict[str, list] = {}
    for pid, s in scores.items():
        if pid == "_rounds":
            continue
        by_model.setdefault(str(s.get("model") or "?"), []).append(pid)
    models_html = "".join(
        f"<li><div><strong>{html.escape(_short_model(m))}</strong> "
        f"<span class='dim'>{len(pids)} post(s)</span></div>"
        f"<div class='dim'>{' · '.join('P' + str(p) for p in sorted(pids)[:8])}"
        f"{' …' if len(pids) > 8 else ''}</div></li>"
        for m, pids in sorted(by_model.items(), key=lambda kv: -len(kv[1])))
    return (f"<h1>Ratings</h1>"
            f"<p class='dim'>Every post with its rubric score and the model that "
            f"generated/evaluated it. Rubric: honesty, thesis, scenario logic, "
            f"Persian, attribution, hook — max 10; 8+ is publish-ready.</p>"
            f"<section><h2>Score distribution</h2>{chart}</section>"
            f"<section><h2>Per model</h2><ul class='airy'>{models_html}</ul></section>"
            f"<section><h2>Posts ({len(rows)})</h2><ul class='airy'>{''.join(rows)}</ul></section>")
