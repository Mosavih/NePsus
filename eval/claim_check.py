"""P3: claim-level verification (FActScore-style, adapted, audit R1/R2).

The existing verify_numbers() checks that every number in the post appears
somewhere in the dossier TEXT. That is necessary but weak: it cannot tell WHICH
evidence backs a claim, so a number can pass while dangling free of its subject.

This module parses the dossier's evidence-ID structure and builds a number ->
evidence-id map:
  [MEASUREMENTS 47,48,...] label        + following "series: period:value, ..."
      -> those ids back every (period, value) number pair on the series line
  [FINDING 12] (meta) statement         -> id 12 backs the numbers in its text

verify_claims(post, dossier_text) then decomposes the post into sentences
(the atomic-claim unit) and returns, per numbered sentence, which evidence ids
back each number. A sentence where ANY number has no backing id is flagged.

Bidirectionality: ids only come from the dossier itself, so "every used id
exists" holds by construction; the real check is direction one (post->evidence).

Used two ways:
  - CLI on published posts: python eval/claim_check.py posts/post_4_v4.txt
  - run_v03 prints a one-line report after compose (report-only for now:
    verify_numbers already blocks fabricated numbers; this adds the binding
    audit and will gate once the mapping language is tuned on real dossiers).
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _norm_num(s: str) -> str:
    """Normalize a Persian/European digit string to a comparable float string."""
    s = s.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789"))
    s = s.replace(",", "").replace("٫", ".").rstrip(".")
    try:
        v = float(s)
    except ValueError:
        return ""
    # 111.9 and 111,928,863,188 are different claims; keep raw float repr
    return repr(v)


EN_NUMWORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
               "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
               "eleven": "11", "twelve": "12", "dozen": "12", "twenty": "20",
               "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
               "seventy": "70", "eighty": "80", "ninety": "90", "hundred": "100"}


def _normalize_numwords(s: str) -> str:
    """'five to seven days' -> '5 to 7 days' so the tokenizer sees the numbers
    journalistic claims spell out (live finding on P16's Vietnam reserve).
    Magnitude words multiply: '2 million' -> '2000000' (P17: the rial claim
    reads '2 million rials per dollar' while the post cites ۲,۰۰۰,۰۰۰)."""
    def rep(m):
        return EN_NUMWORDS.get(m.group(0).lower(), m.group(0))
    s = re.sub(r"[A-Za-z]+", rep, s)

    def _mag(m):
        try:
            v = float(m.group(1))
        except ValueError:
            return m.group(0)
        mult = {"thousand": 1e3, "million": 1e6, "billion": 1e9,
                "trillion": 1e12}[m.group(2).lower()]
        return str(int(v * mult)) if v * mult == int(v * mult) else str(v * mult)
    return re.sub(r"(\d+(?:\.\d+)?)\s+(thousand|million|billion|trillion)\b",
                  _mag, s, flags=re.I)


def _pct_candidates(k: str, bound_values: list[float]) -> set:
    """If k looks like a percent, accept it when it equals the percent change
    between two bound values (a sentence may legitimately COMPUTE growth from
    two series points it cites; live finding: 116.6% from 2020->2024 exports)."""
    out = set()
    try:
        v = float(k)
    except ValueError:
        return out
    if not (0 < v < 10000):
        return out
    for a in bound_values:
        for b in bound_values:
            if a and abs(a) > 1e-9 and a != b:
                chg = (b - a) / abs(a) * 100
                if abs(chg - v) < 0.15:
                    out.add(f"{a}->{b}")
    return out


def build_evidence_index(dossier_text: str) -> dict:
    """number-key -> set(evidence ids). Also returns id->description."""
    num_to_ids: dict[str, set] = {}
    id_desc: dict[str, str] = {}
    lines = dossier_text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        cm = re.match(r"\[CLAIM (\d+)\]\s*\[[^\]]*\]\s*(.*)", ln)
        if cm:
            cid = int(cm.group(1))
            text = _normalize_numwords(cm.group(2))
            id_desc[f"C{cid}"] = text[:80]
            for tok in _number_tokens(text):
                k = _norm_num(tok)
                if k:
                    num_to_ids.setdefault(k, set()).add(f"C{cid}")
            # continuation line (source url) has no numbers; skip
            i += 1
            continue
        m = re.match(r"\[MEASUREMENTS ([\d,]+)\]\s*(.*)", ln)
        if m:
            ids = [x for x in m.group(1).split(",") if x]
            desc = m.group(2)[:80]
            # series lines follow until blank line
            j = i + 1
            series_pairs: list[tuple[float, float]] = []
            while j < len(lines) and lines[j].startswith("  "):
                sl = lines[j]
                if sl.strip().startswith("series:"):
                    # pairs "2020:51664876276" separated by ", "
                    for pair in sl.split(":", 1)[1].split(","):
                        if ":" in pair:
                            period, val = pair.split(":", 1)
                            try:
                                series_pairs.append((float(period), float(val)))
                            except Exception:
                                pass
                            for tok in (period.strip(), val.strip()):
                                k = _norm_num(tok)
                                if k:
                                    num_to_ids.setdefault(k, set()).update(ids)
                    for pid in ids:
                        id_desc.setdefault(f"M:{pid}", desc)
                j += 1
            # pct-derivation index: a post may cite growth computed FROM the
            # series without repeating both absolute values (live P16: 116.6%
            # growth 2020->2024). Bind every pairwise pct change to these ids.
            for a_i in range(len(series_pairs)):
                for b_i in range(a_i + 1, len(series_pairs)):
                    p0, v0 = series_pairs[a_i]
                    p1, v1 = series_pairs[b_i]
                    if v0 and abs(v0) > 1e-9 and v0 != v1:
                        chg = (v1 - v0) / abs(v0) * 100
                        for key in (f"{chg:.4g}", f"{chg:.2f}", f"{chg:.1f}"):
                            num_to_ids.setdefault(key, set()).update(ids)
            i = j
            continue
        m = re.match(r"\[FINDING (\d+)\]\s*(.*)", ln)
        if m:
            fid = m.group(1)
            desc = m.group(2)[:100]
            id_desc.setdefault(f"F:{fid}", desc)
            # numbers in the finding statement (and one continuation line)
            body = m.group(2)
            if i + 1 < len(lines) and lines[i + 1].startswith("  "):
                body += " " + lines[i + 1]
            for tok in re.findall(r"\d+(?:[.,]\d+)?", body):
                k = _norm_num(tok)
                if k:
                    num_to_ids.setdefault(k, set()).add(fid)
            i += 1
            continue
        i += 1
    return {"num_to_ids": num_to_ids, "id_desc": id_desc}


def _with_tol_candidates(k: str) -> set:
    """Keys within rounding tolerance of k (matches verify_numbers tolerance)."""
    out = set()
    try:
        v = float(k)
    except ValueError:
        return out
    out.add(k)
    return out


def _number_tokens(s: str) -> list[str]:
    """Digit runs INCLUDING thousand separators, so ۵۱,۶۶۴,۸۷۶,۲۷۶ is one
    token, not three fragments. Persian comma-grouping is part of the number."""
    out = []
    for m in re.finditer(r"\d+(?:[,،.]\d+)*", s or ""):
        out.append(m.group(0))
    return out


def verify_claims(post: str, dossier_text: str) -> dict:
    idx = build_evidence_index(dossier_text)
    num_to_ids = idx["num_to_ids"]
    prose = re.split(r"📚", post or "")[0]
    sents = [s.strip() for s in re.split(r"(?<=[.!؟])\s+", prose) if s.strip()]
    report = []
    n_backed = n_partial = 0
    for s in sents:
        toks = _number_tokens(s)
        if not toks:
            continue
        per_num = {}
        bound_vals: list[float] = []   # values that DID bind, for pct-derivation
        for tok in toks:
            k = _norm_num(tok)
            ids = num_to_ids.get(k) if k else None
            if not ids:
                # tolerant match: exact float equality after punctuation strip
                for kk, vv in num_to_ids.items():
                    try:
                        if abs(float(kk) - float(k)) <= max(0.15, abs(float(k)) * 0.005):
                            ids = vv
                            break
                    except Exception:
                        continue
            if ids:
                try:
                    bound_vals.append(float(k))
                except Exception:
                    pass
            per_num[tok] = sorted(ids) if ids else []
        unmapped = [t for t, ids in per_num.items() if not ids]
        if not unmapped:
            n_backed += 1
        else:
            n_partial += 1
        report.append({"sentence": s[:130], "numbers": per_num,
                       "fully_backed": not unmapped})
    return {"n_numbered_sentences": n_backed + n_partial,
            "fully_backed": n_backed, "partially_backed": n_partial,
            "sentences": report}


def main() -> None:
    path = sys.argv[1]
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else None
    txt = open(path, encoding="utf-8").read()
    head, _, body = txt.split("=== GATES")[0].strip().partition("\n\n")
    post = body.strip() or head.strip()
    if pid:
        # rebuild the dossier exactly as the composer saw it
        import sqlite3
        from src.investigation_layer.dossier import (build_dossier,
                                                     format_dossier_for_composer)

        class _DB:
            def _require_connection(self):
                c = sqlite3.connect("nexus_think_tank.db")
                c.row_factory = sqlite3.Row

                class _W:
                    def __init__(self, c): self.c = c
                    def execute(self, *a, **k): return self.c.execute(*a, **k)
                return _W(c)

        dossier_text = format_dossier_for_composer(build_dossier(_DB(), pid))
    else:
        dossier_text = txt
    res = verify_claims(post, dossier_text)
    print(f"numbered sentences: {res['n_numbered_sentences']}  "
          f"fully backed: {res['fully_backed']}  "
          f"partially: {res['partially_backed']}")
    for s in res["sentences"]:
        if not s["fully_backed"]:
            print("  UNBACKED:", s["sentence"])
            for tok, ids in s["numbers"].items():
                if not ids:
                    print(f"    number {tok}: no evidence id")


if __name__ == "__main__":
    main()
