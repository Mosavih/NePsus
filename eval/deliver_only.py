"""Deliver an already-composed post file (card + Telegram) without recomposing.

Usage: python eval/deliver_only.py <post_path> <pid>

For posts that composed+QC-passed but died in the card/send stage (live P25).
Marks published ONLY on confirmed delivery, else reverts marks for retry.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from eval.run_v03 import (deliver_to_telegram, mark_published,
                          pick_visual, revert_publish_marks)


def parse_header(post_path: str) -> tuple[str, list]:
    tldr, gloss = "", []
    with open(post_path, encoding="utf-8") as f:
        for line in f:
            if line.startswith("TLDR:"):
                tldr = line.partition(":")[2].strip()
            elif line.startswith("GLOSSARY:"):
                terms = [t.strip() for t in
                         line.partition(":")[2].split("|")]
                gloss = [{"term": t} for t in terms if t]
            elif line.startswith("OUTLINE:"):
                break
    return tldr, gloss


def main() -> int:
    post_path, pid = sys.argv[1], int(sys.argv[2])
    tldr, gloss = parse_header(post_path)
    print(f"[deliver-only] P{pid} tldr={tldr[:60]!r} terms={[g['term'] for g in gloss]}")
    card_png, _vkind = pick_visual(post_path, pid)
    ok = deliver_to_telegram(post_path, card_png, tldr, gloss)
    if ok:
        mark_published(pid)
        print(f"[deliver-only] P{pid} delivered + marked published")
        return 0
    revert_publish_marks(pid)
    print(f"[deliver-only] P{pid} DELIVERY FAILED -- marks reverted")
    return 1


if __name__ == "__main__":
    sys.exit(main())
