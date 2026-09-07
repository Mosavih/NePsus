"""Deterministic Persian post-normalizer (P2, audit Part 2).

WHY THIS EXISTS
Audit C found across 9 published posts: 16 unjoined «می» verbs (ZWNJ omitted,
e.g. «می شود»), 1 broken fragment «می‌ » (ZWNJ followed by space), 3 western
digit runs inside Persian prose, and long-word sentence pileups. This is the
documented Persian orthography problem in arXiv:2010.00287 (ZWNJ omission).

Design constraints:
- RULE-BASED, no LLM: deterministic, free, testable (the audit doctrine).
- CONSERVATIVE: only unambiguous repairs; no suffix-joining («ها») because
  contexts differ. A wrong "fix" is worse than a left flaw.
- Applied to the post BEFORE the mechanical gates, so gates see final text.

normalise_fa() transforms:
  1. «می‌ » broken fragment  -> «می‌» joined to the next word
  2. «می X» / «نمی X» space  -> «می‌X» / «نمی‌X» (ZWNJ join)
  3. ASCII digits            -> Persian digits (0-9 -> ۰-۹)
  4. space before ، . ؛ : ؟ ! -> removed; duplicated ,،.!! collapsed
  5. multiple spaces          -> single space (line structure preserved)
  6. «ه «» weirdness not touched; ZWNJ placement inside words untouched.

count_issues() reports what a text WOULD get fixed (used by tests + runner).
"""
import re

AR_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

_ZWNJ = "\u200c"

# space-before-punctuation inside Persian text
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([،؛:.؟!])")
_DUP_PUNCT = re.compile(r"([،؛:.؟!])\1+")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

LAST_STATS: dict = {}
# «می»/«نمی» followed by space + Persian letter -> ZWNJ join. Note this also
# catches the «می‌ » broken-fragment case in its second pass (ZWNJ then space).
_MI_SPACE = re.compile(r"(?<![^\s])(ن?می)[ \t]+(?=[\u0600-\u06FF])")
_MI_ZWNJ_SPACE = re.compile(r"(ن?می)\u200c[ \t]+(?=[\u0600-\u06FF])")


def _fix_mi(text: str) -> str:
    # first repair the broken «می‌ » fragment (ZWNJ + space), then bare «می »
    text = _MI_ZWNJ_SPACE.sub(lambda m: m.group(1) + _ZWNJ, text)
    text = _MI_SPACE.sub(lambda m: m.group(1) + _ZWNJ, text)
    return text


# Outline-scaffold lines the composer echoes at the top of post_fa
# ("- ضربه شواهد:", "- فرود:"). Readers must never see them (r17: they leaked
# into the delivered body). Only stripped at TEXT START, never mid-post.
_SCAFFOLD_RE = re.compile(
    r"^\s*-\s*(قلاب|چرخش|ضربه\s*شواهد|شواهد|فرود)\s*:")


def strip_leading_scaffold(text: str) -> str:
    lines = (text or "").splitlines()
    i = 0
    while i < len(lines) and (
            not lines[i].strip() or _SCAFFOLD_RE.match(lines[i])):
        i += 1
    return "\n".join(lines[i:])


_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")


def _num_norm(s: str) -> str:
    s = (s or "").translate(_FA_DIGITS)
    return (s.replace("٬", "").replace(",", "").replace("٫", ".")
             .replace("٪", "").replace("%", ""))


def keystone_missing(post: str, spec: str) -> list:
    """Non-year numbers in the scenario spec absent from the post.

    Years (1900-2100) are skipped: they travel on numbers, prose need not
    repeat them. Everything else (trigger level, keystone share) MUST appear.
    """
    if not spec:
        return []
    post_nums = set(re.findall(r"\d+(?:\.\d+)?", _num_norm(post)))
    missing: list = []
    for n in re.findall(r"\d+(?:\.\d+)?", _num_norm(spec)):
        if "." not in n:
            try:
                if 1900 <= int(n) <= 2100:
                    continue
            except ValueError:
                continue
        if n not in post_nums and n not in missing:
            missing.append(n)
    return missing


def normalise_fa(text: str, metric_sources: dict | None = None,
                 metric_full_names: dict | None = None) -> str:
    if not text:
        return text or ""
    # Cure observability (r24): silent try-blocks hid whether cures ran.
    # Counts land in LAST_STATS; the caller prints them to the run log.
    global LAST_STATS
    LAST_STATS = {"scaffold": 0, "placeholders": 0, "labels": 0,
                  "rounded": 0, "inserted": 0, "skip_prov": 0,
                  "skip_nonum": 0, "defparens": 0,
                  "defappos": 0, "deduped": 0, "yehfix": 0, "dotfix": 0,
                  "splits": 0}
    out = strip_leading_scaffold(text)
    LAST_STATS["scaffold"] = 1 if out != text else 0
    # r32: Latin/spaced decimals ("95.63", "۹۵. ۶۳") let cure regexes match a
    # bare "۹۵." and insert INSIDE the number ("۹۵. (Yahoo)۶۳"). Normalize the
    # separator first, and never let a number match end with one.
    out, LAST_STATS["dotfix"] = re.subn(r"(?<=[۰-۹0-90-9])\.\s*(?=[۰-۹0-90-9])",
                                        "٫", out)
    if metric_sources:
        out, LAST_STATS["placeholders"] = fix_source_placeholders(
            out, metric_sources)
        # Order matters: correct wrong labels BEFORE splitting (splitter
        # must see final sentences), both before orthography.
        out, LAST_STATS["labels"] = correct_source_labels(out,
                                                          metric_sources)
        # r19: round monsters, then source what is still sourceless --
        # both before the splitter so it sees final sentences.
        out, LAST_STATS["rounded"] = round_big_numbers(out)
        if metric_full_names:
            out, LAST_STATS["inserted"], _sk = insert_missing_sources(
                out, metric_full_names)
            LAST_STATS["skip_prov"] = _sk.get("has_provider", 0)
            LAST_STATS["skip_nonum"] = _sk.get("no_number", 0)
        # r20: definitions live in the glossary, not in body parens.
        out, LAST_STATS["defparens"] = strip_definitional_parens(out)
        # r26: same smuggle with em-dashes.
        out, LAST_STATS["defappos"] = strip_definitional_appos(out)
        # r23: source-once -- first (provider, metric) stays, repeats go.
        _HEAD_CACHE[:] = [head_key(nm) for nm in (metric_full_names or {})]
        out, LAST_STATS["deduped"] = dedupe_provider_parens(out)
        # r29: detached ezafe ی ("دلار ی در").
        out, LAST_STATS["yehfix"] = join_detached_yeh(out)
        out, LAST_STATS["splits"] = split_runons(out)
    out = _fix_mi(out)
    out = out.translate(AR_DIGITS)
    # Persian-script separators inside Persian digit runs: ۲۰,۹۱۴.۳ -> ۲۰٬۹۱۴٫۳
    out = re.sub(r"(?<=[۰-۹]),(?=[۰-۹])", "٬", out)
    out = re.sub(r"(?<=[۰-۹])\.(?=[۰-۹])", "٫", out)
    out = _SPACE_BEFORE_PUNCT.sub(r"\1", out)
    out = _DUP_PUNCT.sub(r"\1", out)
    # collapse runs of spaces (but never newlines)
    out = "\n".join(_MULTI_SPACE.sub(" ", ln).strip() for ln in out.split("\n"))
    return out


def source_fa(s: str) -> str:
    """Persian provider name (single home; dossier.py uses this too)."""
    s = s or ""
    if "World Bank" in s or s.strip() == "WDI":
        return "بانک جهانی"
    if "Yahoo" in s:
        return "یاهو فایننس"
    if "IMF" in s:
        return "صندوق بین‌المللی پول"
    if s.startswith("OWID:"):
        return "پایگاه Our World in Data"
    if "SCI" in s:
        return "مرکز آمار ایران"
    if "CBI" in s:
        return "بانک مرکزی ایران"
    return s


_PLACEHOLDER_RE = re.compile(
    r"\[\s*(?:بنا بر\s+)?منبع\s*[:.٫]?\s*[A-Za-zXY۰-۹]?\s*\]|\[\.\.\.\]|…{2,}")

_SAFE_SPLIT_RE = re.compile(
    r"؛\s+(زیرا|چرا که|چون|اما|ولی|با این حال|در حالی که|هرچند)")

# Wrong-provider phrases the model emits under attribution pressure. Any of
# these within a metric's sentence is verified against the map (r13: Brent
# labeled World Bank AND EIA across rounds; "بازار جهانی" weasel).
# (r14: novel paraphrases each round -- "نهادهای معتبر بین‌المللی".)
_PROVIDER_PHRASES = [
    "بانک جهانی", "یاهو فایننس", "Yahoo", "صندوق بین‌المللی پول",
    "مرکز آمار", "بانک مرکزی", "EIA", "داده‌های بازار جهانی",
    "آمارهای رسمی", "بازار جهانی", "نهادهای معتبر بین‌المللی",
    "نهادهای بین‌المللی", "منابع معتبر", "منابع رسمی",
    # r27: the model coined a new evasion -- bare "ارائه‌دهنده" (provider)
    # instead of the provider's name. The corrector swaps it mechanically.
    "ارائه‌دهنده", "ارائه دهنده", "ارائهدهنده",
]


_BIGNUM_RE = re.compile(
    r"[۰-۹0-9][۰-۹0-9٬،,٫.\s]*[۰-۹0-9](?:[٫.][۰-۹0-9]+)?")


def _fa_float(tok: str) -> float | None:
    t = (tok.translate(_FA_DIGITS).replace("٬", "").replace("،", "")
         .replace(",", "").replace(" ", "").replace("٫", "."))
    try:
        return float(t)
    except ValueError:
        return None


def _fa_fmt(v: float) -> str:
    return (f"{v:.1f}".translate(
        str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")).replace(".", "٫"))


def round_big_numbers(text: str) -> tuple[str, int]:
    """Magnitude-aware rounding of full-precision monsters.

    r19: the r1 disease returned (۱۱۱٬۹۲۸٬۸۶۳٬۱۸۸٫۴). >=1e9 -> ~X میلیارد,
    >=1e6 -> ~X میلیون. Only runs of >=7 digits qualify, so years, percents
    and trigger levels pass through untouched.
    r30: smalls with >2 decimals (۲۵٫۷۵۴۴) -> 2 decimals. 1-2dp numbers
    (۹۵٫۶۳، ۵۶٫۴) pass through.
    """
    n = 0

    def _rep(m: re.Match) -> str:
        nonlocal n
        tok = m.group(0)
        if len(re.sub(r"\D", "", tok.translate(_FA_DIGITS))) < 7:
            return tok
        v = _fa_float(tok)
        if v is None or v < 1e6:
            return tok
        n += 1
        if v >= 1e9:
            return "حدود " + _fa_fmt(v / 1e9) + " میلیارد"
        return "حدود " + _fa_fmt(v / 1e6) + " میلیون"

    out = _BIGNUM_RE.sub(_rep, text)

    def _rep_small(m: re.Match) -> str:
        nonlocal n
        tok = m.group(0)
        v = _fa_float(tok)
        if v is None:
            return tok
        n += 1
        return _fa_fmt(round(v + 1e-12, 2))

    out = re.sub(r"[۰-۹0-9][۰-۹0-9٬،,]*[٫.][۰-۹0-9]{3,}", _rep_small, out)
    return out, n


def head_key(name: str) -> str:
    """Distinctive 3-word head of a fa metric name for prose matching.

    Full fa names never appear verbatim; single words collide across metrics
    (r21: "دلار" mapped to the wrong provider, so Brent-as-World-Bank went
    uncorrected). Heads are unique per metric and safe under the
    number+no-provider guards of the callers.
    """
    _w = (name or "").split()
    while _w and _w[0].rstrip(":") in ("نام", "فارسی", "انگلیسی"):
        _w = _w[1:]
    _head = " ".join(_w[:3]) if len(_w) >= 4 else " ".join(_w)
    return _head if len(_head) >= 6 else ""


def insert_missing_sources(text: str, full_map: dict) -> tuple[str, int]:
    """Append the provider after the last number of a metric's sentence.

    r19: the export value appeared with no WB beside it. Full fa names only
    (short-word fallbacks over-fire). Skips sentences that already name any
    provider. One insertion per sentence.
    """
    if not full_map:
        return text, 0
    # r20: full fa names never appear verbatim ("قیمت نفت برنت (دلار/بشکه)
    # — ..."), so match distinctive 3-word heads instead. Heads are unique
    # per metric (verified: قیمت نفت برنت / ارزش دلاری صادرات / سهم صادرات
    # کالا / سهم سوخت از) and number+no-provider guards prevent over-fire.
    keyed: dict = {}
    for nm, src in full_map.items():
        _h = head_key(nm)
        if _h:
            keyed.setdefault(_h, src)
    names = sorted(keyed, key=len, reverse=True)
    if not names:
        return text, 0, {}
    skips: dict = {"has_provider": 0, "no_number": 0, "no_head": 0}
    edits: list = []
    # r28: split on punctuation WITHOUT requiring trailing space -- a
    # missing space after "." merges two sentences and a neighbor's provider
    # wrongly suppresses insertion (prime suspect for the 111.9 miss).
    _spans = []
    _st = 0
    for _m in re.finditer(r"[.؟!\n]+", text):
        _spans.append((_st, _m.end()))
        _st = _m.end()
    _spans.append((_st, len(text)))
    for a, b in _spans:
        sent = text[a:b]
        _has_head = any(nm and nm in sent for nm in names)
        if not _has_head:
            continue
        if any(p in sent for p in _PROVIDER_PHRASES):
            skips["has_provider"] += 1
            continue
        if not re.search(r"[۰-۹0-9]", sent):
            skips["no_number"] += 1
            continue
        for nm in names:
            if nm and nm in sent:
                # r22: insert after the FIRST non-year number (the rule wants
                # the source beside the metric's first number; inserting
                # after the last put Yahoo after ۷۰ instead of ۹۵٫۶۳).
                # Number + trailing unit words (r19b: inserting right after
                # the digits splits "۱۱۱٫۹ | میلیارد دلار" -- swallow the
                # unit tail so the source lands after "دلار").
                _cands = []
                for m in re.finditer(
                        r"[۰-۹0-9][۰-۹0-9٬٫.,]*"
                        r"(?:\s*(?:میلیارد|میلیون|هزار|دلار|درصد|بشکه|تومان"
                        r"|ریال|یورو|سنت))+",
                        sent):
                    _cands.append(m)
                if not _cands:
                    # r32: must end with a DIGIT -- "[0-9.]*)" matched bare
                    # "۹۵." and split the number in half.
                    for m in re.finditer(
                            r"[۰-۹0-9](?:[۰-۹0-9٬٫.,]*[۰-۹0-9])?", sent):
                        _cands.append(m)
                last = None
                for m in _cands:
                    _v = _fa_float(re.sub(
                        r"\s*(?:میلیارد|میلیون|هزار|دلار|درصد|بشکه|تومان"
                        r"|ریال|یورو|سنت)+$", "",
                        m.group(0)))
                    if _v is not None and "." not in str(
                            m.group(0)).translate(_FA_DIGITS).replace(
                            "٫", ".").split()[0] and 1900 <= _v <= 2100:
                        continue
                    last = m
                    break
                if last is None and _cands:
                    last = _cands[-1]
                if last:
                    edits.append((a + last.end(), f" ({keyed[nm]})"))
                break
    for pos, ins in sorted(edits, reverse=True):
        text = text[:pos] + ins + text[pos:]
    return text, len(edits), skips


_DEFPAREN_RE = re.compile(r"\(([^()]{15,})\)")


_DEFAPPO_RE = re.compile(
    r" — ([^—.؟!\n()]{10,}?) در سال (?=[۰-۹0-9])")


def strip_definitional_appos(text: str) -> tuple[str, int]:
    """Delete glossary definitions smuggled as em-dash appositions.

    r26: "سهم سوخت ... — چند درصد صادرات کالایی کشور نفت و گاز است؛
    معیار وابستگی ... در سال ۲۰۲۲..." Only fires on the exact smuggle shape
    (dash + long digit-free phrase + در سال + year); "در سپتامبر"-style
    legitimate dating is untouched.
    """
    n = 0

    def _rep(m: re.Match) -> str:
        nonlocal n
        inner = m.group(1)
        if (len(inner.split()) >= 4
                and not re.search(r"[۰-۹0-9]", inner)
                and not any(p in inner for p in _PROVIDER_PHRASES)):
            n += 1
            return " در سال "
        return m.group(0)

    out = _DEFAPPO_RE.sub(_rep, text)
    return re.sub(r"  +", " ", out), n


def strip_definitional_parens(text: str) -> tuple[str, int]:
    """Delete wordy definition parens; definitions live in the glossary.

    r20: "(مجموع درآمد صادراتی به دلار جاری)" after its metric. Only parens
    with >=5 words, no digits and no provider phrase are removed -- source
    labels (بانک جهانی), units (دلار/بشکه) and short glosses survive.
    """
    n = 0

    def _rep(m: re.Match) -> str:
        nonlocal n
        inner = m.group(1)
        if (len(inner.split()) >= 5
                and not re.search(r"[۰-۹0-9]", inner)
                and not any(p in inner for p in _PROVIDER_PHRASES)):
            n += 1
            return ""
        return m.group(0)

    out = _DEFPAREN_RE.sub(_rep, text)
    return re.sub(r"  +", " ", out), n


_HEAD_CACHE: list = []


def join_detached_yeh(text: str) -> tuple[str, int]:
    """Join detached ezafe ی after units: "۹۵٫۶۳ دلار ی در" -> "دلاری در".

    r29 model typo. The lookahead (space/punct, not ا) keeps "دلار یا"
    (dollar or...) intact.
    """
    out, n = re.subn(r"(دلار|تومان|ریال|یورو|درصد|بشکه) ی(?=[\s.؟!])",
                     r"\1ی", text)
    return out, n


def dedupe_provider_parens(text: str) -> tuple[str, int]:
    """Keep the first (provider) paren per (provider, metric).

    r23: same metric repeating its source (Yahoo twice for Brent) -- remove.
    r29: same provider serving DIFFERENT metrics (WB for fuel share AND
    GDP share) is legitimate -- keep. Keyed on the sentence's metric head.
    """
    n = 0
    seen: set = set()

    def _head_of(start: int) -> str:
        _seg_start = max(text.rfind(".", 0, start),
                         text.rfind("؟", 0, start),
                         text.rfind("!", 0, start),
                         text.rfind("\n", 0, start)) + 1
        return text[_seg_start:start]

    def _rep(m: re.Match) -> str:
        nonlocal n
        inner = m.group(1)
        if inner in _PROVIDER_PHRASES:
            _seg = _head_of(m.start())
            _key = (inner, len(_seg))
            for _h in _HEAD_CACHE:
                if _h and _h in _seg:
                    _key = (inner, _h)
                    break
            if _key in seen:
                n += 1
                return ""
            seen.add(_key)
        else:
            seen.add(inner)
        return m.group(0)

    out = re.sub(r"\(([^()]+)\)", _rep, text)
    return re.sub(r"  +", " ", out), n


def fix_source_placeholders(text: str, metric_sources: dict
                            ) -> tuple[str, int]:
    """Replace source placeholder brackets with the mapped provider.

    metric_sources: {metric fa-name (or distinctive word): provider fa-name}.
    For each bracket, look back 150 chars for a known metric; on hit write
    (provider), else DELETE the bracket (a clean sentence missing attribution
    beats a placeholder; QC8 flags the gap for revision).
    Returns (fixed_text, n_unresolved). Deterministic, no LLM.
    """
    if not text or not metric_sources:
        return text, 0
    names = sorted(metric_sources, key=len, reverse=True)
    unresolved = 0

    def _rep(m: re.Match) -> str:
        nonlocal unresolved
        # Same-sentence window only: a metric two sentences back must not
        # donate its source (live test misattributed a [...] this way).
        seg = max(text.rfind(".", 0, m.start()),
                  text.rfind("؟", 0, m.start()),
                  text.rfind("!", 0, m.start()),
                  text.rfind("\n", 0, m.start()))
        window = text[seg + 1:m.start()]
        for name in names:
            if name and name in window:
                return f"({metric_sources[name]})"
        unresolved += 1
        return ""

    out = _PLACEHOLDER_RE.sub(_rep, text)
    return out, unresolved


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    spans, start = [], 0
    for m in re.finditer(r"[.؟!]\s+|\n+", text):
        spans.append((start, m.end()))
        start = m.end()
    spans.append((start, len(text)))
    return spans


def _fa_len(s: str) -> int:
    return len(re.findall(r"[\u0600-\u06FFA-Za-z0-9٪٫٬]+", s))


def split_runons(text: str, limit: int = 35) -> tuple[str, int]:
    """Split >limit-word sentences ONLY at safe joints (؛ + زیرا/چون/اما...
    -> . + same word). Both halves must exceed 8 words or the split is
    refused (a stranded clause is worse than a long sentence). Returns
    (text, n_splits). Deterministic, meaning-preserving by construction:
    the joint word is kept, only ؛ becomes . and the next word capitalizes
    (Persian has no case: nothing else changes).
    """
    if not text:
        return text, 0
    out, n = [], 0
    for a, b in _sentence_spans(text):
        sent = text[a:b]
        if _fa_len(sent) <= limit:
            out.append(sent)
            continue
        m = _SAFE_SPLIT_RE.search(sent)
        if not m:
            out.append(sent)
            continue
        left, right = sent[:m.start()], sent[m.start() + 1:]
        if _fa_len(left) < 8 or _fa_len(right) < 8:
            out.append(sent)
            continue
        out.append(left.rstrip() + ". " + right.lstrip())
        n += 1
    return "".join(out), n


def correct_source_labels(text: str, metric_sources: dict) -> tuple[str, int]:
    """Replace wrong provider labels with the mapped one.

    For each metric occurrence, any _PROVIDER_PHRASES phrase in the SAME
    sentence that differs from the mapped provider is substituted. Fixes
    r10/r13 misattributions (EIA, World Bank on Brent) mechanically.
    Returns (text, n_corrections).
    """
    if not text or not metric_sources:
        return text, 0
    names = sorted(metric_sources, key=len, reverse=True)
    total, out_parts, pos = 0, [], 0
    for a, b in _sentence_spans(text):
        sent = text[a:b]
        hit = next((nm for nm in names if nm and nm in sent), None)
        if hit:
            want = metric_sources[hit]
            for ph in _PROVIDER_PHRASES:
                if ph != want and ph in sent:
                    sent = sent.replace(ph, want)
                    total += 1
        out_parts.append(sent)
    return "".join(out_parts), total


def count_issues(text: str) -> dict:
    """How many fixable defects the text currently carries (pre-normalisation)."""
    t = text or ""
    return {
        "mi_space": len(_MI_SPACE.findall(t)) + len(_MI_ZWNJ_SPACE.findall(t)),
        "western_digits": len(re.findall(r"[0-9]", t)),
        "space_before_punct": len(_SPACE_BEFORE_PUNCT.findall(t)),
        "multi_space": len(_MULTI_SPACE.findall(t)),
    }


def sentence_stats(text: str) -> tuple[int, int, float]:
    """(n_sentences, max_words, avg_words) over the prose (glossary excluded)."""
    prose = re.split(r"📚", text or "")[0]
    sents = [s for s in re.split(r"[.!؟]\s*", prose) if s.strip()]
    words = [len(s.split()) for s in sents]
    if not words:
        return 0, 0, 0.0
    return len(words), max(words), sum(words) / len(words)
