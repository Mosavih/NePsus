"""Panel tests: routes render, reads are real, live paths untouched (stdlib only)."""
import os
import sys
import time
from pathlib import Path

import pytest

PANEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PANEL))
REPO = PANEL.parent

import read
import templates
from app import page


def live_mtimes():
    targets = [REPO / "nexus_think_tank.db"] + sorted((REPO / "var").glob("*.json"))
    return {str(p): p.stat().st_mtime for p in targets if p.exists()}


def test_routes_render():
    for path in ["/", "/update", "/problems", "/pipeline", "/jobs", "/quality"]:
        code, body, ctype = page(path)
        assert code == 200, path
        assert "NePsus panel" in body, path
    code, _, _ = page("/nope")
    assert code == 404


def test_reads_hit_real_data():
    f = read.funnel_counts()
    assert f["problems"] >= 48
    assert f["measurements"] > 1000
    srcs = read.sources_status()
    assert len(srcs) >= 2
    q = read.posts_queue()
    assert len(q) >= 8  # 28 live posts as of 2026-09-07 (filesystem truth)
    assert any(i["card"] for i in q)
    ch = read.channels()
    names = " ".join(c["name"] for c in ch)
    assert "Guardian" in ch[0]["detail"] or "Guardian" in names
    assert any("OpenAlex" in c["name"] for c in ch)
    assert read.job_progress() == {"running": False} or "frac" in read.job_progress()


def test_post_parser_human_sections():
    import read as read_mod
    d = read_mod.post_detail(1)
    p = d["parsed"]
    assert p["angle"] and p["tldr"] and len(p["body"]) >= 2
    assert p["qc"].get("qc_pass") is True
    assert p["note"]
    body = templates.post_page(d)
    assert "ANGLE:" not in body and "=== V0.3 ===" not in body
    assert "خلاصه" in body and "Quality check" in body


def test_live_paths_untouched():
    before = live_mtimes()
    time.sleep(0.05)
    read.funnel_counts()
    read.sources_status()
    read.update_state()
    read.posts_queue()
    read.quality_state()
    for path in ["/", "/update", "/problems", "/pipeline", "/jobs", "/quality"]:
        page(path)
    after = live_mtimes()
    assert before == after


def test_confirm_pages_explain():
    for action in ["cancel", "pause", "resume"]:
        body = templates.confirm_page(action)
        assert "<form" in body and "Confirm" in body
    for action in ["endorse", "reject"]:
        assert "Missing problem id" in templates.confirm_page(action)
        body = templates.confirm_page(action, "4")
        assert "P4" in body and "<form" in body


def test_cancel_when_idle_is_harmless():
    import actions
    st = actions.worker_state()
    if st["pid"] is not None and st["alive"]:
        pytest.skip("a worker is genuinely running; refusing to touch it in tests")
    assert actions.do_cancel() in (
        "no worker on record", "stale worker record cleared — nothing was running")


def test_pause_resume_roundtrip_restores():
    import actions
    was_paused = actions.is_paused()
    try:
        actions.do_pause()
        assert actions.is_paused()
        actions.do_resume()
        assert not actions.is_paused()
    finally:
        if was_paused:
            actions.do_pause()
    assert actions.is_paused() == was_paused


def test_endorse_is_state_only():
    import actions
    from state import connect
    before = live_mtimes()
    actions.do_endorse(99999, "panel test")
    try:
        from state import get_decisions
        assert get_decisions()[99999]["decision"] == "endorsed"
        assert live_mtimes() == before
    finally:
        conn = connect()
        conn.execute("DELETE FROM decision WHERE pid=99999")
        conn.commit()
        conn.close()


def test_post_detail_real():
    import read as read_mod
    d = read_mod.post_detail(1)
    assert d is not None
    assert d["texts"], "post_1 text files exist"
    assert read_mod.post_detail(99999) is None
    code, body, _ = page("/post")
    assert code == 200  # route registered (detail via query handled in do_GET)


def test_post_img_allowlist():
    from app import POST_IMG_RE
    assert POST_IMG_RE.match("post_1_v4_card.png")
    assert POST_IMG_RE.match("post_27_v4.txt")
    assert not POST_IMG_RE.match("../eval/nexus_bot.py")
    assert not POST_IMG_RE.match("post_1_v4.exe")


def test_run_refuses_busy_slot_without_touching_live():
    import os
    import actions
    before = live_mtimes()
    real = actions._read_pid
    actions._read_pid = lambda: os.getpid()  # always "alive", no files touched
    try:
        assert "Refused" in actions.run_tracked("run-propose")
        assert "Refused" in actions.run_tracked("run-compose", ["4"])
    finally:
        actions._read_pid = real
    assert live_mtimes() == before


def test_run_compose_needs_numeric_pid():
    import actions
    real = actions._read_pid
    actions._read_pid = lambda: None
    try:
        assert "numeric" in actions.run_tracked("run-compose", ["abc"])
    finally:
        actions._read_pid = real


def test_llm_env_roundtrip_and_backup():
    import llm
    env_before = llm.ENV_F.read_text(encoding="utf-8")
    try:
        msg = llm.set_env_key("ROUTER_MODEL_TEST_KEY", "prov/model-x")
        assert "Saved" in msg
        assert "ROUTER_MODEL_TEST_KEY=prov/model-x" in llm.ENV_F.read_text(encoding="utf-8")
        backups = sorted(llm.ROOT.glob("var/env_backup_*"))
        assert backups, "backup written"
        assert "ROUTER_MODEL_TEST_KEY" not in backups[-1].read_text(encoding="utf-8")
    finally:
        llm.ENV_F.write_text(env_before, encoding="utf-8")
    import state
    conn = state.connect()
    conn.execute("DELETE FROM action_log WHERE target='ROUTER_MODEL_TEST_KEY'")
    conn.commit()
    conn.close()


def test_llm_effective_models_matches_pipeline():
    import llm
    stages = {s["key"]: s["current"] for s in llm.effective_models()}
    assert "extract" in stages and "compose" in stages
    for combo in stages.values():
        assert combo in llm.COMBOS, combo
    assert llm.fallback_chain(), "fallback chain parses"


def test_routes_page_renders_with_data():
    import llm
    body, stages = templates.routes_page(llm.effective_models(),
                                         llm.fallback_chain(), {},
                                         {"MostComplicated": {
                                             "fails": 3, "error": "429 x3",
                                             "disabled_until": "2099-01-01T00:00:00+00:00",
                                             "note": "panel"}})
    assert "Stage assignments" in body and len(stages) >= 11
    assert "MostComplicated" in body and "quarantined" in body
    assert "Kill 12 h" in body
    edit = templates.route_edit_page(llm.effective_models(), "compose")
    assert "Save to .env" in edit and "MostComplicated" in edit
    assert "Unknown stage" in templates.route_edit_page(llm.effective_models(), "nope")


def test_set_stage_combo_roundtrip():
    import llm
    env_before = llm.ENV_F.read_text(encoding="utf-8")
    try:
        msg = llm.set_stage_combo("cards", "Mechanical")
        assert "Saved" in msg
        assert "ROUTER_COMBO_CARDS=Mechanical" in llm.ENV_F.read_text(encoding="utf-8")
        bad = llm.set_stage_combo("cards", "Nonexistent")
        assert "unknown combo" in bad
    finally:
        llm.ENV_F.write_text(env_before, encoding="utf-8")
    import state
    conn = state.connect()
    conn.execute("DELETE FROM action_log WHERE action='stage-assign'")
    conn.commit()
    conn.close()
