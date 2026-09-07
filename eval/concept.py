"""Concept visuals: atmospheric AI images for posts with no backed hero stat.

Data cards carry exact numbers and must never be AI-generated (models mangle
Persian text and digits). But a no_stat post has no number to show -- its old
thesis card was decorative text-on-background. For those, a text-free
conceptual image (Pollinations, free, no key) is more attractive and honest.

Usage: python eval/concept.py <post_path> <pid>  -> prints jpg path or ''
Mode choice lives in run_v03.pick_visual (no_stat -> concept, else data card).
"""
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def _angle(post_path: str) -> str:
    with open(post_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("ANGLE:"):
                return line.partition(":")[2].strip()
    return ""


def build_prompt(angle: str) -> str:
    """One cheap LLM call: Persian angle -> English image prompt. Template
    fallback keeps this working when the router stalls (the v3 lesson: every
    enhancement must degrade, never block)."""
    fb = ("Editorial illustration, solemn atmosphere, Iranian city at dusk, "
          "muted colors, no text, no words, no letters, no numbers, "
          "photorealistic")
    try:
        sys.path.insert(0, ".")
        from eval.discover import chat_resilient
        txt = chat_resilient([{"role": "user", "content":
                "Write ONE English image prompt (max 30 words) for an editorial "
                "illustration matching this article angle. ATMOSPHERIC, no people "
                "close-ups, absolutely NO text/words/letters/numbers in the image. "
                "Reply with the prompt only.\nANGLE (Persian): " + angle[:300]}],
            temperature=0.7, timeout=60, task="visual")
        p = (txt or "").strip()[:300]
        if len(p) < 15:
            return fb
        if not re.search(r"[a-zA-Z]{3,}", p):
            return fb
        p += ", no text, no words, no letters, no numbers"
        return p
    except Exception:
        return fb


def make_concept(post_path: str, pid: int) -> str | None:
    prompt = build_prompt(_angle(post_path))
    print(f"  [concept] prompt: {prompt[:100]}", flush=True)
    out = f"posts/post_{pid}_concept.jpg"
    q = urllib.parse.quote(prompt)
    url = (f"https://image.pollinations.ai/prompt/{q}"
           f"?width=1080&height=1080&model=flux&nologo=true&seed={pid}")
    for attempt in range(1, 4):
        try:
            p = subprocess.run(
                ["curl", "-sL", "--max-time", "150", "-A", "NexusThinkTank/0.1",
                 "-o", out, url], capture_output=True, text=True, timeout=170)
            if os.path.exists(out) and os.path.getsize(out) > 20000:
                with open(out, "rb") as f:
                    magic = f.read(4)
                if magic[:2] == b"\xff\xd8" or magic[:4] == b"\x89PNG":
                    print(f"  [concept] OK {out}", flush=True)
                    return out
        except Exception as e:
            print(f"  [concept] attempt {attempt}: {type(e).__name__}",
                  flush=True)
        time.sleep(10)
    print("  [concept] FAILED -- caller falls back to data card", flush=True)
    return None


if __name__ == "__main__":
    r = make_concept(sys.argv[1], int(sys.argv[2]))
    print(r or "")
