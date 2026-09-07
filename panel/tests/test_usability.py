
"""Usability regressions 2026-09-07: propose visibility, update/propose split,
reject->archive."""
import sys
from pathlib import Path

PANEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PANEL))
REPO = PANEL.parent

import read
import templates
from app import page


def test_problems_queue_lists_live_problems():
    items = read.problems_queue()
    assert items, "problems table is non-empty"
    assert {"id", "statement", "status"} <= set(items[0])
    code, body, _ = page("/problems")
    assert code == 200 and "Problems" in body


def test_reject_archives_and_restores():
    import actions
    import sqlite3
    from state import connect, get_decisions
    pid = 99998
    try:
        msg = actions.do_reject(pid, "panel test")
        assert "archived" in msg
        conn = sqlite3.connect(REPO / "nexus_think_tank.db")
        try:
            st = conn.execute("SELECT status FROM problems WHERE id=?", (pid,)).fetchone()
            assert st is None  # scratch pid: no live row touched
        finally:
            conn.close()
        assert get_decisions()[pid]["decision"] == "rejected"
        # rejected pid vanishes from the merged list
        _, probs_body, _ = page("/problems")
        assert f"Problem {pid}" not in probs_body
        arch = templates.problems_page(read.problems_queue(), read.posts_queue(),
                                        get_decisions(), True)
        # scratch pid is not in live problems, so archive shows others/nothing — just renders
        assert "Archived" in arch or "archived" in arch
        msg2 = actions.do_restore(pid, "panel test")
        assert "restored" in msg2
        assert get_decisions()[pid]["decision"] == "restored"
    finally:
        conn = connect()
        conn.execute("DELETE FROM decision WHERE pid=?", (pid,))
        conn.commit()
        conn.close()


def test_update_and_propose_do_not_overlap():
    import actions
    update_cmds = [c for seq in actions.RUNNABLE["run-update"] for c in seq]
    propose_cmds = [c for seq in actions.RUNNABLE["run-propose"] for c in seq]
    assert any("refresh_macro" in c for c in update_cmds)
    assert not any("discover" in c for c in update_cmds), "update must not propose"
    assert any("discover" in c for c in propose_cmds)
    assert not any("refresh_macro" in c for c in propose_cmds), "propose must not refresh"
    assert "--dry" not in propose_cmds


def test_confirm_probe_and_quarantine_render():
    # 2026-09-07B: probe-tier confirm KeyError'd on t['label'] and dropped
    # the connection; route-disable/enable confirm dropped the route param
    # so Kill/Re-enable silently did nothing.
    body = templates.confirm_page("probe-tier", "", {"tier": "Mechanical"})
    assert "Mechanical" in body and "Probe now" in body
    body = templates.confirm_page("probe-tier")
    assert "Probe now" in body
    body = templates.confirm_page("route-disable", "", {"route": "Mechanical"})
    assert "Mechanical" in body and "name='route'" in body
    body = templates.confirm_page("route-enable", "", {"route": "Mechanical"})
    assert "Mechanical" in body
    import llm as llm_mod
    assert llm_mod.probe_tier("no-such-thing")["ok"] is False


def test_problems_both_views_render():
    from app import problems_body
    assert "open problem(s)" in problems_body(False)
    assert "back to open problems" in problems_body(True)
