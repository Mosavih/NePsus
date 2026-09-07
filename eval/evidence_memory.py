"""A4: evidence-level anti-repetition.

User finding (2026-09-02): new posts reused the same macro numbers as older
posts (unemployment in 4 problems, GDP growth in 4...), making the desk feel
generic. Mechanism: for every PUBLISHED post, record which indicators its
problem presented (problem_snapshots already holds this). At compose time,
inject a 'RECENTLY USED' line into the context pack: indicators headlined in
the last N posts, with an instruction to lead with DIFFERENT evidence unless
the reused number is genuinely central.

record_headline(pid): append indicator->post mapping to the ledger (called by
run_v03 after publish). recently_used(conn, exclude_pid): set of indicators
used in the last 5 published posts. context_note(): the Persian writer line.
"""
import json
import os
import sqlite3

LEDGER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "var", "editorial_ledger.json")


def record_headline(pid: int, indicators: list[str]) -> None:
    """Append the indicators this post's problem presented to the ledger."""
    led = []
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            led = json.load(f)
    # update the newest entry for this pid (created at publish time)
    for r in reversed(led):
        if r.get("problem_id") == pid:
            r["indicators"] = indicators
            break
    else:
        from datetime import datetime, timezone
        led.append({"date": datetime.now(timezone.utc).isoformat(),
                    "problem_id": pid, "indicators": indicators})
    with open(LEDGER, "w", encoding="utf-8") as f:
        json.dump(led, f, ensure_ascii=False, indent=1)


def recently_used(exclude_pid: int | None = None, n: int = 5) -> list[dict]:
    """Indicators headlined in the last n published posts (newest first)."""
    if not os.path.exists(LEDGER):
        return []
    with open(LEDGER, encoding="utf-8") as f:
        led = json.load(f)
    out = []
    for r in reversed(led):
        if r.get("problem_id") == exclude_pid:
            continue
        for ind in r.get("indicators") or []:
            out.append({"indicator": ind, "problem_id": r.get("problem_id"),
                        "date": r.get("date")})
        if len(out) >= 40:
            break
    return out[:40]


def context_note(exclude_pid: int | None = None) -> str:
    """Persian instruction line for the writer's context pack."""
    used = recently_used(exclude_pid)
    if not used:
        return ""
    names = sorted({u["indicator"] for u in used})
    lines = ["شاخص‌هایی که پست‌های اخیر همین کانال قبلاً برجسته کرده‌اند "
             "(تکرارِ محوریِ همین اعداد، پست را کلیشه‌ای می‌کند):"]
    for nm in names:
        lines.append(f"- {nm}")
    lines.append("اگر عددی از این فهرست در استدلال تو لازم است اشکالی ندارد، "
                 "اما محورِ عددیِ پست باید شاخصِ تازه‌ای باشد که اخیراً "
                 "برجسته نشده — یا زاویه‌ای کاملاً تازه از همان شاخص.")
    return "\n".join(lines)


def snapshot_indicators(conn: sqlite3.Connection, pid: int) -> list[str]:
    """The indicators this problem's latest snapshot holds."""
    conn.row_factory = sqlite3.Row
    return [r["indicator"] for r in conn.execute("""
        SELECT DISTINCT indicator FROM problem_snapshots WHERE problem_id=?""",
        (pid,))]
