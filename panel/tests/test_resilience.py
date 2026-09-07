"""Root-level resilience tests: the pipeline skips quarantined routes.

These exercise src/route_health directly (the shared ledger), then verify the
two wiring points a dead-NIM scenario depends on: gate2's candidate list and
the ledger file the panel reads.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))

from src import route_health as rh  # noqa: E402


def test_quarantine_after_two_fails_and_recovery():
    route = "test/dead-route"
    try:
        rh.record_fail(route, "429 grinding", note="test")
        assert rh.usable(route), "single fail is only a warning"
        rh.record_fail(route, "429 grinding")
        assert not rh.usable(route), "two fails = quarantined"
        data = json.loads(rh.HEALTH_F.read_text(encoding="utf-8"))
        assert data[route]["disabled_until"]
        rh.record_ok(route)
        assert rh.usable(route), "success clears quarantine"
        assert json.loads(rh.HEALTH_F.read_text(encoding="utf-8"))[route]["fails"] == 0
    finally:
        data = rh._load()
        data.pop(route, None)
        rh._save(data)


def test_manual_disable_and_enable():
    route = "test/manual-route"
    try:
        rh.set_manual(route, disabled=True, hours=12, note="panel")
        assert not rh.usable(route)
        msg = rh.set_manual(route, disabled=False)
        assert "re-enabled" in msg.lower() or route in msg
        assert rh.usable(route)
    finally:
        data = rh._load()
        data.pop(route, None)
        rh._save(data)


def test_healthy_candidates_demotes_not_drops():
    route = "test/order-route"
    try:
        rh.set_manual(route, disabled=True, hours=1)
        out = rh.healthy_candidates([route, "live/route"])
        assert out[-1] == route, "quarantined goes last, never removed"
        assert out[0] == "live/route"
    finally:
        data = rh._load()
        data.pop(route, None)
        rh._save(data)


def test_gate2_candidates_include_alias_and_chain():
    import src.gate2_extraction as g2
    cands = g2._route_candidates()
    assert cands[0] == g2._get_router_config()["model"]
    # combo edition: all four combos reachable + legacy aliases kept
    for combo in ("Complicated", "Medium.Supporter", "Mechanical",
                  "MostComplicated"):
        assert combo in cands, combo
    assert "C1" in cands and "opncde" in cands


def test_resilient_call_walks_past_dead_route():
    import src.gate2_extraction as g2

    calls = []

    def fake_builder(route, timeout):
        calls.append(route)

    class FakeMsg:
        content = "ok"

    class FakeChoice:
        message = FakeMsg()

    class FakeResp:
        choices = [FakeChoice()]

    def fake_create(**kw):
        route = kw["model"]
        if route == "test/first-dead":
            raise RuntimeError("429")
        return FakeResp()

    # monkeypatch client behavior through build_client return value
    class FakeCompletions:
        create = staticmethod(fake_create)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    def builder(route, timeout):
        calls.append(route)
        return FakeClient()

    text, used = rh.call_llm_resilient(
        builder, [{"role": "user", "content": "hi"}],
        ["test/first-dead", "test/then-live"])
    assert used == "test/then-live" and text == "ok"
    data = rh._load()
    assert data["test/first-dead"]["fails"] >= 1
    assert data["test/then-live"]["fails"] == 0
    for r in ("test/first-dead", "test/then-live"):
        rh._load().pop(r, None)
        d = rh._load()
        d.pop(r, None)
        rh._save(d)
