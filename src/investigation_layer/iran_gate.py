"""Step 48 -- Iran-compatibility / plausibility gate (problem level).

Decides, for each discovered Problem, whether it is in-scope for an
Iran-focused think tank. This is the explicit gate the user asked for: the
corpus is currently Iran-scoped only INCIDENTALLY (sources are Tehran Times +
Al Jazeera Iran coverage), never ENFORCED. This gate persists a verdict
(iran_relevant: yes | no | partial) + a short iran_note (plausibility
justification) on the problems table.

Verdict semantics:
  - yes     : the problem is substantively about Iran (domestic condition,
              Iranian actor, or Iran directly affected).
  - partial : the problem is NOT about Iran per se, but has a clear, material
              Iran linkage or spillover (e.g. external regulatory bodies
              restricting Iranian exports, global shock hitting Iran). Keep,
              but flag the linkage.
  - no      : the problem is not about Iran and has no material Iran linkage.
              Route OUT of the Iran-scoped pipeline.

Conservative on 'no': only mark 'no' when the statement truly has no Iran
connection. When in doubt between 'partial' and 'no', prefer 'partial' so we
do not silently drop a problem that could matter to Iran.

Deterministic + cached (uses the LLM cache from extraction). Returns a dict.
"""
from __future__ import annotations


_IRAN_SYS = (
    "You are the Iran-scope gate for a think tank that ONLY publishes problems "
    "relevant to Iran. Given a Problem statement (and its topic and any "
    "affected entities), decide its Iranian relevance.\n"
    "Return ONLY JSON: {\"iran_relevant\": str, \"iran_note\": str}.\n"
    "iran_relevant must be exactly one of: \"yes\", \"partial\", \"no\".\n"
    "- \"yes\": substantively about Iran (Iranian domestic condition, Iranian "
    "actor/entity, or Iran directly and centrally affected).\n"
    "- \"partial\": not about Iran per se, but has a clear, material Iran "
    "linkage or spillover (external body restricting Iranian exports, a global "
    "shock that hits Iran, a comparator that informs Iranian policy). Keep it.\n"
    "- \"no\": not about Iran and no material Iran linkage. Route out.\n"
    "Be CONSERVATIVE on 'no': only use it when there is genuinely no Iran "
    "connection. When uncertain between 'partial' and 'no', choose 'partial'.\n"
    "iran_note: one concise sentence justifying the verdict (cite the specific "
    "Iran linkage, or state plainly why none exists). No hedging."
)


def classify_problem_iran(
    statement: str,
    topic: str | None = None,
    affected_entities: list[str] | None = None,
) -> dict:
    """Return {'iran_relevant': str, 'iran_note': str} for a problem statement.

    Cached via the shared LLM cache (keyed on model+system+user).
    """
    # Lazy import to avoid a circular import with gate2_extraction, which
    # imports this module at package-load time.
    from .extraction import _chat, _parse_json_block

    ctx = f"TOPIC: {topic or 'unknown'}\n"
    if affected_entities:
        ctx += "AFFECTED ENTITIES: " + ", ".join(affected_entities[:12]) + "\n"
    user = f"PROBLEM STATEMENT:\n{statement}\n\n{ctx}\nAssess Iranian relevance."
    try:
        raw = _chat(_IRAN_SYS, user, temperature=0)
        data = _parse_json_block(raw)
        verdict = (data.get("iran_relevant") or "").strip().lower()
        if verdict not in ("yes", "partial", "no"):
            verdict = "partial"  # conservative default
        note = (data.get("iran_note") or "").strip()
        return {"iran_relevant": verdict, "iran_note": note}
    except Exception:
        # Defensive: on any failure, do NOT drop the problem. Mark partial with
        # a note so it stays in-scope rather than being silently excluded.
        return {
            "iran_relevant": "partial",
            "iran_note": "Iran-gate call failed; defaulting to partial to avoid silent exclusion.",
        }
