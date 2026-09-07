"""Step 50 -- Engaging, scientifically-grounded Telegram post renderer (+ diagram).

Assembles a concise, sober-scientific post for a Problem from:
  - the problem statement (Discovery)
  - its Iran-compatibility verdict (Step 48)
  - its brief (Step 44: evidence-grounded findings/measurements)
  - its solution analysis (Step 49: levers + candidate interventions + gaps)

Output: Telegram-friendly Markdown. Structure:
  # Title (problem, one line, plain language)
  > one-sentence hook (why it matters to Iran)
  ## What the evidence says   (measurements/findings, cited by id)
  ## How it could be addressed (levers + interventions, flagged)
  ## What we don't yet know    (gaps -- honest)
  [DIAGRAM] ASCII flow: Problem -> Evidence -> Lever -> Expected effect
            (+ optional mermaid block, rendered on web only)

Discipline:
  - No LLM prose by default (deterministic assembly). Every claim traces to an
    evidence id from the brief/solution analysis.
  - Findings/interventions with missing Iran-relevance are flagged 'unverified'.
  - Gaps are always shown; a post is NEVER closed as 'solved'.
  - Tone: concise, accurate, scientific, engaging-but-sober (no clickbait).

render_post(db, problem_id, with_mermaid=False) -> dict {markdown, diagram_ascii,
mermaid, quality_flags}. The quality_flags let Step 51 gate it.
"""
from __future__ import annotations

from src.investigation_layer.brief import render_problem_brief
from src.investigation_layer.synthesis import build_solution_analysis


def _ascii_diagram(problem_stmt: str, levers: list, findings_n: int) -> str:
    """Render a box-drawing flow: Problem -> Evidence -> Lever -> Effect."""
    W = 38  # inner box width
    def box(text: str) -> str:
        t = text[:W]
        return f"│ {t:<{W}} │"

    prob = problem_stmt[:W]
    ev = f"{findings_n} evidence item(s)" if findings_n else "no direct evidence"
    lever_txt = "levers identified" if levers else "lever: indicative only"
    top = "┌" + "─" * (W + 2) + "┐"
    bot = "└" + "─" * (W + 2) + "┘"
    arrow = "│" + " " * (W // 2 + 1) + "▼"
    lines = [
        top,
        box(f"PROBLEM: {prob}"),
        bot,
        arrow,
        top,
        box(f"EVIDENCE: {ev}"),
        bot,
        arrow,
        top,
        box(f"LEVER: {lever_txt}"),
        bot,
        arrow,
        top,
        box("EXPECTED EFFECT: mitigated outcome"),
        bot,
    ]
    return "\n".join(lines)


def _mermaid(problem_stmt: str, levers: list) -> str:
    n_levers = len(levers)
    nodes = ["P[Problem]", "E[Evidence]", "L[Lever]", "X[Expected effect]"]
    edges = ["P --> E", "E --> L", "L --> X"]
    body = "\n  ".join(nodes + edges)
    return f"```mermaid\nflowchart TD\n  {body}\n```"


def render_post(db, problem_id: int, with_mermaid: bool = False) -> dict:
    conn = db._require_connection()
    prow = conn.execute(
        "SELECT statement, iran_relevant, iran_note FROM problems WHERE id=?",
        (problem_id,),
    ).fetchone()
    if not prow:
        raise ValueError(f"problem {problem_id} not found")
    statement = prow["statement"]
    iran = prow["iran_relevant"] or "pending"

    # Skip non-Iran problems at render time (the gate's job).
    if iran == "no":
        return {"markdown": "", "diagram_ascii": "", "mermaid": "",
                "quality_flags": ["excluded: iran_relevant=no"], "rendered": False}

    brief = render_problem_brief(db, problem_id)
    sa = build_solution_analysis(db, problem_id)

    # Count evidence items from the brief (measurements + findings cited).
    import re
    ev_ids = re.findall(r"\[(measurement|finding):\d+\]", brief)
    findings_n = len(ev_ids)

    quality_flags = []
    if sa.confidence == "indicative":
        quality_flags.append("indicative: no finding-derived lever (evidence pending)")
    weak = [i for i in sa.candidate_interventions if not i["verified"]]
    if weak:
        quality_flags.append(f"{len(weak)} unverified intervention(s) flagged")

    # ---- Assemble Markdown ----
    md = []
    # Title: problem as a plain-language headline.
    md.append(f"🇮🇷 **{statement.strip()}**")
    md.append("")
    md.append(f"_{iran_note_line(prow['iran_note'], iran)}_")
    md.append("")
    # Evidence section (from brief, trimmed to the substantive part).
    md.append("## 📊 What the evidence says")
    ev_block = _evidence_block(brief)
    md.append(ev_block)
    # Transparency: if the linked evidence carries no Iran geo tag, say so --
    # comparative evidence is legitimate but must not read as Iran-specific.
    if "[finding:" in ev_block or "[measurement:" in ev_block:
        import re as _re
        geo_tags = _re.findall(r"geo=([^)]+)\)", ev_block)
        iran_tagged = any("iran" in g.lower() for g in geo_tags)
        has_measurements = "Iran |" in ev_block or "| Iran" in ev_block
        if not iran_tagged and not has_measurements:
            md.append("")
            md.append("_Evidence base: international comparators "
                      "(no Iran-specific study linked yet)._")
            quality_flags.append("comparative-evidence-only")
    md.append("")
    # Solution section.
    md.append("## 🛠️ How it could be addressed")
    md.append(_solution_block(sa))
    md.append("")
    # Gaps.
    md.append("## 🔍 What we don't yet know")
    md.append(_gaps_block(sa))
    md.append("")
    # Diagram.
    md.append("## 🔗 At a glance")
    diag = _ascii_diagram(statement, sa.evidence_levers, findings_n)
    md.append(f"```\n{diag}\n```")
    if with_mermaid:
        md.append("")
        md.append(_mermaid(statement, sa.evidence_levers))

    return {
        "markdown": "\n".join(md),
        "diagram_ascii": diag,
        "mermaid": _mermaid(statement, sa.evidence_levers) if with_mermaid else "",
        "quality_flags": quality_flags,
        "rendered": True,
        "iran_relevant": iran,
    }


def iran_note_line(note, iran) -> str:
    if iran == "partial":
        return f"Linked to Iran: {note}" if note else "Linked to Iran (spillover)."
    if iran == "yes":
        return f"Iran-focused: {note}" if note else "Iran-focused problem."
    return "Iran relevance pending."


def _evidence_block(brief: str) -> str:
    """Extract the 'Direct evidence' + 'Deterministic patterns' parts of the
    brief, trimmed + DEDUPED (the pipeline links one finding to every scholarly
    Question, so the per-Question brief repeats evidence), for the post.
    Falls back to EVIDENCE PENDING."""
    if "EVIDENCE PENDING" in brief:
        return "_Evidence pending — no measurements or findings linked yet._"
    out, seen = [], set()
    patterns = []
    inferences = []
    capture = None  # None | 'pat' | 'inf'
    for line in brief.splitlines():
        s = line.strip()
        if s.startswith("**Direct evidence**"):
            capture = None
            continue
        if s.startswith("**Deterministic patterns**"):
            capture = "pat"
            continue
        if s.startswith("**Supported inferences**"):
            capture = "inf"
            continue
        if s.startswith("**"):  # any other section ends capture
            capture = None
            continue
        if not s.startswith("- "):
            continue
        key = s[:120]
        if s.startswith("- [measurement") or s.startswith("- [finding"):
            if key not in seen:
                seen.add(key)
                out.append(s)
        elif capture == "pat":
            if s not in patterns:
                patterns.append(s)
        elif capture == "inf":
            if s not in inferences:
                inferences.append(s)
    if patterns:
        out.append("**Deterministic patterns**")
        out.extend(patterns)
    if inferences:
        out.append("**Supported inferences**")
        out.extend(inferences)
    return "\n".join(out[:12]) if out else "_Evidence pending._"


def _solution_block(sa) -> str:
    out = []
    if sa.evidence_levers:
        out.append("_Evidence-grounded levers:_")
        seen = set()
        for l in sa.evidence_levers[:6]:
            lev = l["implied_lever"]
            if not lev or lev in seen:
                continue
            seen.add(lev)
            out.append(f"- {lev} _(finding {l['from_finding']})_")
        # Dedupe identical lever texts across findings (same intervention named
        # by several findings -> one line, first finding id cited).
    # Only show VERIFIED candidate interventions in the public post (noise filter:
    # unverified entries are almost always extraction artifacts, not levers).
    verified = [i for i in sa.candidate_interventions if i["verified"]]
    if verified:
        out.append("_Candidate interventions from the literature (Iran-relevance assessed):_")
        for i in verified[:4]:
            rel = f" — Iran: {i['relevance_to_iran']}" if i["relevance_to_iran"] else ""
            out.append(f"- {i['name']}{rel}")
    if not out:
        out.append("_No directly-relevant intervention evidence yet; analysis is indicative._")
    return "\n".join(out)


def _gaps_block(sa) -> str:
    if not sa.gaps:
        return "_None flagged — synthesis is evidence-grounded._"
    return "\n".join(f"- {g}" for g in sa.gaps)
