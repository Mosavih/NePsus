"""Send a composed draft to the admin via @NePsusbot with review buttons.

Usage: python eval/bot_forward.py <pid> [ask|pub]
Same callback_data format the bot understands (approve_<tag>_<pid>).
"""
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PROXY = "http://127.0.0.1:10809"


def _env_token() -> str:
    p = os.path.expandvars(r"%LOCALAPPDATA%\hermes\.env")
    for line in open(p, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        if k.strip() == "NEXUS_BOT_TOKEN":
            return v.strip()
    raise SystemExit("NEXUS_BOT_TOKEN missing")


def _api(token: str, method: str, params: dict):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data)
    req.set_proxy("127.0.0.1:10809", "http")
    req.set_proxy("127.0.0.1:10809", "https")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    pid = sys.argv[1]
    tag = sys.argv[2] if len(sys.argv) > 2 else "ask"
    tok = _env_token()
    admin = json.load(open("var/bot_admin.json", encoding="utf-8"))["admin_id"]
    post_path = f"posts/post_{pid}_v4.txt"
    visual = {}
    try:
        visual = json.load(open(f"var/visual_{pid}.json", encoding="utf-8"))
    except Exception:
        pass
    card = visual.get("path") or post_path.replace(".txt", "_card.png")
    kind = visual.get("kind", "data")

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sp", os.path.expanduser("~/send_post.py"))
    sp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sp)
    _cap, body = sp.post_file_caption_body(post_path)
    parts = sp.split_with_numbering(body, 3800)
    kb = {"inline_keyboard": [
        [{"text": "تأیید + امضا ✅", "callback_data": f"approve_{tag}_{pid}"},
         {"text": "حذف 🗑", "callback_data": f"delete_{tag}_{pid}"}]]}

    if os.path.exists(card):
        out = subprocess.run(
            ["curl", "-s", "--max-time", "90", "-x", PROXY,
             "-F", f"chat_id={admin}", "-F", f"photo=@{card}",
             f"https://api.telegram.org/bot{tok}/sendPhoto"],
            capture_output=True, text=True, timeout=100)
        print("photo:", out.stdout[:120])
    # header so the draft is identifiable outside the bot's own flow
    _api(tok, "sendMessage", {"chat_id": admin,
                              "text": f"پیش‌نویس P{pid} (از سؤال شما) — منتشر نشده."})
    for part in parts:
        _api(tok, "sendMessage", {"chat_id": admin, "text": part})
    _api(tok, "sendMessage", {"chat_id": admin,
                              "text": f"داوری P{pid}:",
                              "reply_markup": json.dumps(kb)})
    print("draft forwarded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
