"""T4+T5 -- Engagement/quality reviewer (LLM rubric) + mechanical fabrication guard.

review_post(): LLM rubric critique of a composed Persian post against its
dossier. Returns verdict PASS | REVISE with concrete, actionable revision
instructions and per-axis scores.

verify_numbers(): DETERMINISTIC. Extracts every number from the post and checks
it appears in the dossier text (normalized: Persian/European digits, commas).
A number that exists nowhere in the dossier = fabricated -> automatic REVISE
with the offending numbers listed.

The loop lives in compose_with_review() (pipeline below): compose -> verify ->
critique -> revise (max N rounds) -> final verdict.
"""
from __future__ import annotations

import os
import re

from src.investigation_layer.extraction import _client

REVIEWER_MODEL = "gemini/gemini-3.5-flash-lite"

REVIEWER_SYS = (
    "You are the senior editorial reviewer for a Persian science-policy "
    "Telegram channel for general (non-academic) readers. You judge a draft "
    "post against its evidence dossier. You are strict about fabrication and "
    "honesty, and demanding about readability. Answer ONLY with JSON."
)

REVIEWER_USR_TMPL = """داسیه شواهد:
--- شروع ---
{dossier}
--- پایان ---

پست پیشنهادی (فارسی):
--- شروع ---
{post}
--- پایان ---

این پست را از ۵ منظر بسنج و فقط JSON برگردان:
{{
  "hook": 0-10,            // قلاب آغازین: آیا مخاطب عام را می‌گیرد؟
  "narrative": 0-10,       // روایت روان؛ نه گزارش خشک داده‌ای
  "clarity": 0-10,         // بدون اصطلاح تخصصی توضیح‌نشده؛ جمله‌های کوتاه
  "grounding": 0-10,       // وفاداری به شواهد؛ هیچ عدد/ادعای بیرونی
  "engagement": 0-10,      // جذابیت کلی برای مخاطب عام تلگرام
  "overall": 0-10,
  "verdict": "PASS" | "REVISE",
  "fabrication_suspects": ["هر عدد یا ادعایی که در داسیه نیست"],
  "dry_spots":  ["جاهایی که هنوز خشک/مکانیکی است"],
  "news_misuse": ["هر عدد/ادعای آماری که فقط از NEWS CONTEXT آمده و در داده‌ها نیست (باید خالی باشد مگر تخلف آشکار)"],
  "relevance": {{"score": 0-10, "note": "آیا پست به مسئله مهم و جاری امروز ایران مربوط است؟ پیوند با خبر روز دارد؟"}},
  "cohesion": {{"score": 0-10, "note": "آیا همه بخش‌ها به یک روایت واحد می‌رسند یا پست تکه‌تکه است؟"}},
  "revision_instructions": ["دستور بازنویسی مشخص و اجرایی"]
}}

آستانه: میانگین clamp شده روی محورها >= 7.5 و بدون fabrication => PASS.
سخت‌گیر باش: پست «کافی» قبول نکن؛ باید واقعاً خواندنی و جالب باشد."""


# ---------------- T5: deterministic fabrication guard ----------------
_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _norm(s: str) -> str:
    s = s.translate(_PERSIAN_DIGITS)
    s = s.replace(",", "").replace("،", "")
    return s


def extract_numbers(text: str) -> list:
    t = _norm(text)
    out = set()
    # decimals / integers / percentages (incl. +/- prefixes)
    for m in re.finditer(r"[+-]?\d+(?:\.\d+)?%?", t):
        out.add(m.group(0))
    return sorted(out)


def verify_numbers(post: str, dossier_text: str) -> dict:
    """Every number in the post must appear in the dossier text."""
    dn = _norm(dossier_text)
    bad = []
    for num in extract_numbers(post):
        bare = num.rstrip("%").lstrip("+")
        if bare in dn or num in dn:
            continue
        # tolerate rounding to 1 decimal vs raw values (e.g. 111.9 vs 111,928,...)
        try:
            v = float(bare)
        except ValueError:
            bad.append(num)
            continue
        # search for a number in dossier within rounding tolerance
        tol_ok = False
        for dm in re.finditer(r"\d+(?:\.\d+)?", dn):
            try:
                dv = float(dm.group(0))
            except ValueError:
                continue
            if dv == 0:
                continue
            if abs(dv - v) <= max(0.15, abs(v) * 0.005):
                tol_ok = True
                break
            # billions/trillions shorthand: 51.7 میلیارد vs 51,664,876,275
            scale = 1_000_000_000 if abs(v) < 10000 else None
            if scale and abs(dv - v * scale) <= dv * 0.01:
                tol_ok = True
                break
        if not tol_ok:
            bad.append(num)
    return {"ok": not bad, "unmatched": bad}


# ---------------- Mechanical sanity gate (V0.1) ----------------
# LLM-free checks: wrong/stale dates presented as current, past-dated "forecasts",
# and claims contradicting a series trend.

import re as _re2
from datetime import date as _dtdate

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")


def _post_years(post):
    """Extract 4-digit years from a Persian/English mixed post."""
    t = post.translate(_PERSIAN_DIGITS)
    return sorted({int(m.group(1)) for m in _re2.finditer(
        r"\b(1[89]\d{2}|20[0-9]\d)\b", t)})


def sanity_check(post, dossier_text):
    """Mechanical date/trend coherence gate. Returns {ok, violations[]}."""
    v = []
    today = _dtdate.today()
    this_yr = today.year

    # 1. No future years.
    for y in _post_years(post):
        if y > this_yr:
            v.append(f"year {y} is in the future (today is {today.isoformat()})")

    # 2. FUTURE-year mentions must be labeled as forecast. The current year
    # itself is legitimately mentioned as "now" (news dates, TODAY line), so
    # only strictly-future years require the label. (V0.3 fix: 2026-as-now
    # false positive killed P3 twice.)
    tnorm = post.translate(_PERSIAN_DIGITS)
    for y in _post_years(post):
        if y > this_yr:
            idx = tnorm.find(str(y))
            window = tnorm[max(0, idx - 70):idx + 70] if idx >= 0 else ""
            if not any(k in window for k in ("پیش‌بینی", "پیش بینی", "برآورد",
                                             "پروژکشن", "forecast", "projection")):
                v.append(f"year {y} looks like a projection but is not labeled "
                         f"as forecast/estimate")

    # 3. Trend contradiction vs TEMPORAL FACTS lines in the dossier text.
    dn = dossier_text.lower()
    pl = post.lower()
    claims = {"increasing": ["افزایش", "رو به رشد", "جهش", "بالاتر رفت"],
              "decreasing": ["کاهش", "نزولی", "سقوط", "پایین آمد"]}
    for sm in _re2.finditer(r"temporal facts \(authoritative[^)]*\): ([^\n]+)", dn):
        facts = sm.group(1)
        dm = _re2.search(r"\b(increasing|decreasing|mixed)\b", facts)
        if not dm or dm.group(1) == "mixed":
            continue
        direction = dm.group(1)
        nums_in_facts = set(_re2.findall(r"-?\d+\.\d+", facts))
        shared = [n for n in nums_in_facts if n in post]
        if not shared:
            continue
        opposite = "decreasing" if direction == "increasing" else "increasing"
        for kw in claims[opposite]:
            i = pl.find(kw)
            if i >= 0 and any(n in pl[max(0, i - 200):i + 200] for n in shared):
                v.append(f"possible trend contradiction: series is '{direction}' "
                         f"but post suggests '{opposite}' near {shared[:3]}")
                break

    return {"ok": not v, "violations": v}


# ---------------- T4: LLM reviewer ----------------
def review_post(post: str, dossier_text: str, model: str | None = None,
                pace: float = 0.0) -> dict:
    from src.investigation_layer.models import combo_for
    client = _client()
    models = ([model] if model else []) + [
        m for m in FALLBACK_MODELS if not model or m != model]
    # Primary model = the review combo; FALLBACK_MODELS serve as last resorts.
    primary = combo_for("review")
    if primary not in models:
        models = [primary] + models
    resp = _llm_with_retry(
        client,
        [{"role": "system", "content": REVIEWER_SYS},
         {"role": "user", "content": REVIEWER_USR_TMPL.format(
             dossier=dossier_text, post=post)}],
        0.2,
        models=models, pace=pace,
    )
    raw = (resp.choices[0].message.content or "").strip()
    # robust JSON extraction
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"verdict": "REVISE", "overall": 0,
                "error": f"no JSON in reviewer output: {raw[:200]}",
                "revision_instructions": [raw[:500]]}
    import json
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"verdict": "REVISE", "overall": 0,
                "error": "reviewer JSON parse failed",
                "revision_instructions": [raw[:500]]}
    d.setdefault("verdict", "REVISE")
    d.setdefault("revision_instructions", [])
    d.setdefault("fabrication_suspects", [])
    return d


# ---------------- T6: FINAL quality-check pass (V0.1) ----------------
# Distinct from the review loop (which judges hook/narrative/engagement): QC is
# the final proofread that signs off on mechanical correctness before publish.
QC_SYS = (
    "You are the FINAL quality-check editor for a Persian science-policy "
    "Telegram channel. You do a cold proofread of the drafted Persian post "
    "against its evidence dossier. You check ONLY mechanical correctness and "
    "publish-readiness. Answer ONLY with JSON."
)

QC_USR_TMPL = """شواهد:
--- شروع ---
{dossier}
--- پایان ---

پست نهایی (فارسی):
--- شروع ---
{post}
--- پایان ---

فقط بررسی کیفیت نهایی (proofread) را انجام بده، نه ارزیابی محتوایی:
1. درستی فارسی: غلط املایی/انشایی، نویسه‌های نیم‌فاصله، کسره اضافه، تکرار کلمه.
2. ردیابی عدد: آیا هر عدد در پست دقیقاً در شواهد هست؟ (اگر عددی در شواهد نیست،
   آن را در fabrication_suspects لیست کن)
3. قلاب: اولین ۱-۲ خط آیا مخاطب عام را می‌گیرد؟
4. پلاکholder یا متن ناتمام (مثل «[...]» یا «تکمیل شود») وجود ندارد؟
5. طول: بین ۱۲۰۰ تا ۱۹۰۰ کاراکتر؟
6. تناقض درونی یا ادعای بدون پشتیبانی وجود ندارد؟
7. سبک فارسی (به‌اندازهٔ غلط املایی جدی — این‌ها در persian_errors می‌آیند
و باعث بازنویسی می‌شوند): جملهٔ بالای ~۳۵ کلمه؛ «به عنوان» دو بار در یک
جمله؛ اصطلاح عامیانه (دست‌وپا زدن و مانندش) در متن تحلیلی؛ پرانتز چندسطری؛
قید «برآورد اولیه» بیش از یک بار در یک پاراگراف؛ عدد بزرگ با ارقام کامل
به‌جای گرد («۱۱۱٬۹۲۸٬۸۶۳٬۱۸۸» به‌جای «حدود ۱۱۱٫۹ میلیارد دلار»)؛ تکرار نام
سنجه داخل پرانتز («قیمت نفت برنت (نفت برنت)»)؛ پرانتزِ تعریفِ سنجه («سهم
صادرات (چند درصد اقتصاد...)») — تعریف مال واژه‌نامه است نه متن؛ جای‌نگهدار
([منبع]، [...]، X/Y). هر مورد را با نقل‌قول کوتاه از همان جمله لیست کن.
8. انتساب یک‌باره (چک‌لیست صریح، تک‌تک سنجه‌ها): برای هر سنجه‌ای که در پست
عدد دارد (مثلاً برنت، ارزش صادرات، سهم صادرات، تورم)، دقیقاً بنویس «نام
منبع کنار عدد: هست/نیست». اگر برای حتی یک سنجه نام منبع (بانک جهانی،
یاهو، مرکز آمار...) کنار اولین عددش نیامده، همان را در persian_errors
بیاور. عبارت‌های کلیِ بدون نامِ ارائه‌دهنده («داده‌های بازار جهانی»،
«آمارهای رسمی»، «برآورد ارائه‌دهنده») به‌مثابهٔ نبود منبع‌اند.
سپس برای هر «(منبع: X)» در متن، X را با بخش شواهد چک کن: اگر شواهد
سنجه را به ارائه‌دهندهٔ دیگری نسبت داده (مثلاً برنتِ یاهو به نام EIA)، آن
را در fabrication_suspects بیاور — حدسِ مدل از دانش عمومی، جعل است.
تکرار نام منبع یا سال در ادامهٔ متن، و هر پاراگراف یا پرانتز جداگانه
دربارهٔ قدمت داده‌ها — از جمله الگوی «توجه داشته باشید آخرین...» —
(مگر نبودِ دادهٔ تازه برای تزِ «اکنون»)، در persian_errors بیاید.

JSON برگردان:
{{
  "qc_pass": true | false,
  "persian_errors": ["مورد اصلاح"],
  "fabrication_suspects": ["عدد بیرون از شواهد"],
  "hook_ok": true | false,
  "length_ok": true | false,
  "signoff": "یک جمله درباره آمادگی انتشار"
}}"""


def qc_post(post: str, dossier_text: str, model: str | None = None,
            pace: float = 0.0) -> dict:
    """Dedicated final proofread/quality-check pass."""
    from src.investigation_layer.models import combo_for
    client = _client()
    models = ([model] if model else []) + [
        m for m in FALLBACK_MODELS if not model or m != model]
    # Primary model = the review combo; FALLBACK_MODELS serve as last resorts.
    primary = combo_for("review")
    if primary not in models:
        models = [primary] + models
    resp = _llm_with_retry(
        client,
        [{"role": "system", "content": QC_SYS},
         {"role": "user", "content": QC_USR_TMPL.format(
             dossier=dossier_text, post=post)}],
        0.2, models=models, pace=pace)
    raw = (resp.choices[0].message.content or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"qc_pass": False, "error": f"no JSON in QC output: {raw[:200]}",
                "persian_errors": [], "fabrication_suspects": [],
                "hook_ok": False, "length_ok": False,
                "signoff": "QC produced no parseable verdict"}
    import json
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"qc_pass": False, "error": "QC JSON parse failed",
                "persian_errors": [], "fabrication_suspects": [],
                "hook_ok": False, "length_ok": False,
                "signoff": "QC JSON unparsable"}
    # V0.2 hardening: normalize fuzzy key spellings from the model
    # (e.g. 'signsign' -> 'signoff', 'fabrications' -> 'fabrication_suspects').
    _KEY_ALIASES = {
        "signsign": "signoff", "sign_off": "signoff", "signature": "signoff",
        "signsignoff": "signoff",
        "fabrications": "fabrication_suspects",
        "fabrication": "fabrication_suspects",
        "persian": "persian_errors", "grammar_errors": "persian_errors",
        "hook": "hook_ok", "length": "length_ok",
    }
    for k in list(d.keys()):
        kk = str(k).strip().lower()
        if kk in _KEY_ALIASES and _KEY_ALIASES[kk] not in d:
            d[_KEY_ALIASES[kk]] = d.pop(k)
    d.setdefault("persian_errors", [])
    d.setdefault("fabrication_suspects", [])
    if "qc_pass" not in d:
        d["qc_pass"] = bool(d.get("signoff"))
    return d


FALLBACK_MODELS = [
    m.strip() for m in os.environ.get(
        "ROUTER_FALLBACK",
        "gemini/gemini-3.6-flash,"
        "OR/deepseek/deepseek-v4-flash-free,"
        "nvidia/minimaxai/minimax-m3,"
        "gemini/gemini-3.5-flash,"
        "gemini/gemini-3.5-flash-lite,"
        "gemini/gemini-3.1-flash-lite-preview,"
        "gemini/gemma-4-26b-a4b-it",
    ).split(",") if m.strip()
]


def _llm_with_retry(client, messages, temperature, models=None,
                    max_retries=3, pace: float = 0.0):
    """Router calls under load throw 429/503/timeouts. Retry with backoff, and
    on a per-model daily-quota wall (429 with 'quota'), fall back to the next
    free-tier model so the pipeline keeps moving.

    Combo edition: models are combo aliases walked health-first (quarantined
    combos go last via the shared ledger); every attempt records health.

    `pace`: seconds to sleep BEFORE every attempt (success or retry). Lets us
    stay under the router's rate limit when running many posts in a row
    (V0.1 scaling: quality > quantity, so we space calls out).
    """
    import time
    from src.route_health import (healthy_candidates, record_fail, record_ok,
                                  COMBO_FALLBACK_ORDER, FALLBACK_ALIASES)
    models = models or (COMBO_FALLBACK_ORDER + FALLBACK_ALIASES)
    # TOTAL BUDGET (2026-09-05): models × retries multiplied pathologically
    # (7 models × 3 attempts × 300s timeout ≈ 105 min for ONE call; live r13
    # sat silent 45+ min). Cap total attempts; fail fast under systemic
    # outage instead of hanging the round. Env-tunable.
    import os as _os5
    budget = int(_os5.environ.get("ROUTER_MAX_ATTEMPTS", "6"))
    last = None
    used = 0
    for model in healthy_candidates(list(models)):
        for attempt in range(max_retries):
            if used >= budget:
                break
            used += 1
            if pace:
                time.sleep(pace)
            try:
                resp = client.chat.completions.create(
                    model=model, messages=messages, temperature=temperature)
                record_ok(model)
                return resp
            except Exception as e:  # noqa: BLE001
                last = e
                msg = str(e)
                record_fail(model, f"{type(e).__name__}: {msg[:120]}")
                if "402" in msg or ("quota" in msg.lower() and "429" in msg):
                    break  # paid/credit wall or daily quota -> next model
                if any(t in msg for t in ("429", "503", "502", "timeout",
                                          "Connection", "RateLimit")):
                    time.sleep(6.0 * (attempt + 1))
                    continue
                raise
    raise last


def compose_with_review(db, problem_id: int, composer_model: str | None = None,
                        max_rounds: int = 2, pace: float = 12.0) -> dict:
    """Full loop: compose -> numeric guard -> review -> revise -> ... -> final QC.

    `pace`: seconds slept before each LLM call (default 12s) so a full run of
    several problems stays under the free-tier router rate limit. Quality >
    quantity: we are happy to wait.
    """
    from src.investigation_layer.composer import (
        build_dossier_and_text, COMPOSER_SYS, COMPOSER_USR_TMPL, _client,
        DEFAULT_COMPOSER_MODEL, pick_format, FORMAT_BLOCKS,
    )
    from src.investigation_layer.relevance import (
        resolve_case, format_analysis_block,
    )
    from src.investigation_layer.insight import (
        propose_insights, validate_insights, format_insight_block,
    )
    from src.investigation_layer.ledger import avoid_block
    dossier_text, d = build_dossier_and_text(db, problem_id)
    if not d["coverage"]["has_evidence"]:
        return {"ok": False, "reason": "no evidence", "problem_id": problem_id}
    # CROSS-ATTEMPT FEEDBACK (2026-09-05): run_v03 saves the previous
    # attempt's QC errors to var/qc_feedback_<pid>.txt; they join the dossier
    # so the next roll fixes KNOWN flaws instead of rolling fresh ones.
    import os as _os
    _fbp = f"var/qc_feedback_{problem_id}.txt"
    if _os.path.exists(_fbp):
        try:
            _fb = open(_fbp, encoding="utf-8").read().strip()[:1200]
            if _fb:
                dossier_text += ("\n\nیادداشت منتقد از پیش‌نویس قبلی همین پست "
                                 "(این ایرادها را تکرار نکن):\n" + _fb)
        except Exception:
            pass

    case = resolve_case(d)
    if case["case"] == "discard":
        return {"ok": False, "problem_id": problem_id,
                "discarded": True, "reason": case["reason"],
                "history": [{"round": 0, "case": case}]}
    ablock = format_analysis_block(case["analysis_items"])
    if ablock:
        dossier_text = dossier_text + "\n\n" + ablock
    else:
        dossier_text = dossier_text + (
            "\n\nOWN FORECAST (پیش‌بینی خودمان): بر اساس همین داده‌ها و روندشان، "
            "یک انتظار کوتاه‌مدت منطقی بنویس (جهت حرکت شاخص‌ها پس از بحران)، با "
            "ذکر این که داده رسمی پس از بحران موجود نیست. عدد جدید ممنوع.")
    dossier_text = f"CASE: {case['case']} ({case['reason']})\n\n" + dossier_text

    # V0.2 insight pass: find the non-obvious BEFORE composing.
    try:
        ins_valid = validate_insights(
            propose_insights(dossier_text, model=composer_model, pace=pace),
            dossier_text)
    except Exception:
        ins_valid = []
    iblock = format_insight_block(ins_valid)
    if iblock:
        dossier_text = dossier_text + "\n\n" + iblock
    ablock2 = avoid_block()
    if ablock2:
        dossier_text = dossier_text + "\n\n" + ablock2

    d2_format = pick_format(d)
    fmt_block = FORMAT_BLOCKS.get(d2_format, FORMAT_BLOCKS["narrative"])

    client = _client()
    model = composer_model or DEFAULT_COMPOSER_MODEL
    history = []

    post = None
    feedback = ""
    for rnd in range(1, max_rounds + 1):
        if post is None:
            usr = COMPOSER_USR_TMPL.format(dossier=dossier_text,
                                           fmt_block=fmt_block)
        else:
            usr = (COMPOSER_USR_TMPL.format(dossier=dossier_text, fmt_block=fmt_block)
                   + "\n\nنسخه قبلی پست:\n--- شروع ---\n" + post
                   + "\n--- پایان ---\n\nنقد سردبیر:\n" + feedback
                   + "\n\nبر اساس این نقد، پست را از نو بنویس (فقط متن نهایی پست).")
        resp = _llm_with_retry(client,
                               [{"role": "system", "content": COMPOSER_SYS},
                                {"role": "user", "content": usr}],
                               0.7 if rnd == 1 else 0.4, pace=pace)
        post = (resp.choices[0].message.content or "").strip()
        if post.startswith("```"):
            post = post.strip("`")
            if post.startswith("text"):
                post = post[4:]
            post = post.strip()

        guard = verify_numbers(post, dossier_text)
        san = sanity_check(post, dossier_text)
        guard["sanity"] = san
        rev = review_post(post, dossier_text, pace=pace)
        rev["numeric_guard"] = guard
        history.append({"round": rnd, "review": rev})

        fab_fail = (not guard["ok"] or san["ok"] is False
                    or bool(rev.get("fabrication_suspects")))
        score = float(rev.get("overall") or 0)
        def _sub_score(key):
            node = rev.get(key) or {}
            try:
                return float(node.get("score") or 0)
            except (TypeError, ValueError):
                return 0
        rel_score = _sub_score("relevance")
        coh_score = _sub_score("cohesion")
        passes_review = (rev.get("verdict") == "PASS" and not fab_fail
                         and score >= 7.5 and not rev.get("news_misuse")
                         and rel_score >= 6 and coh_score >= 7)
        if passes_review:
            # V0.1 final quality-check gate before publish.
            qc = qc_post(post, dossier_text, pace=pace)
            if qc.get("qc_pass") and not qc.get("fabrication_suspects") \
                    and not qc.get("persian_errors"):
                return {"ok": True, "problem_id": problem_id,
                        "insights": [v["claim"] for v in ins_valid],
                        "post_fa": post,
                        "rounds": rnd, "final_review": rev, "qc": qc,
                        "history": history, "model": model, "dossier": d,
                        "format": d2_format}
            # QC found fixable issues -> feed back as revision.
            qc_fb = []
            if qc.get("persian_errors"):
                qc_fb.append("اصلاحات فارسی: " + "؛ ".join(qc["persian_errors"]))
            if qc.get("fabrication_suspects"):
                qc_fb.append("این اعداد در شواهد نیستند: "
                             + "، ".join(qc["fabrication_suspects"]))
            if not qc.get("hook_ok"):
                qc_fb.append("قلاب اولیه قوی‌تر باشد.")
            if qc_fb:
                fb = qc_fb
                feedback = "\n".join(f"- {x}" for x in fb)
                continue
            # QC unparsable/errored -> treat as soft pass (don't block publish on
            # a tooling failure, but flag it).
            return {"ok": True, "problem_id": problem_id, "post_fa": post,
                    "rounds": rnd, "final_review": rev, "qc": qc,
                    "history": history, "model": model, "dossier": d,
                    "format": d2_format, "qc_warn": qc.get("error")}
        # Build revision feedback.
        fb = []
        if fab_fail:
            fb.append("این اعداد در شواهد نیستند و باید حذف یا تصحیح شوند: "
                      + "، ".join(sorted(set(guard["unmatched"])
                                         | set(rev.get("fabrication_suspects", [])))))
        fb += [str(x) for x in rev.get("revision_instructions", [])]
        feedback = "\n".join(f"- {x}" for x in fb)

    return {"ok": False, "problem_id": problem_id, "post_fa": post,
            "rounds": max_rounds, "final_review": history[-1]["review"],
            "history": history, "model": model, "dossier": d,
            "reason": "did not pass review within round limit"}


# ---------------- V0.3: outline-then-write, gates go silent ----------------

# Acronyms must be standalone tokens: KC-135 / F-16 / B-52 are equipment model
# numbers, not jargon, so a trailing hyphen+digits disqualifies the match.
ACRONYM_RE = re.compile(r"(?<![A-Za-z0-9\-])([A-Z]{2,6})(?![A-Za-z]*[\-–][0-9])")
FOREIGN_TERM_RE = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")

# Acronyms a general Iranian news reader already knows -- glossing these is noise.
WELL_KNOWN = {
    "IMF", "GDP", "OPEC", "UN", "EU", "NATO", "WHO", "USA", "US", "UK", "CIA",
    "FBI", "NASA", "BBC", "CNN", "AI", "IT", "PC", "TV", "USD", "EUR", "GMT",
    "COVID", "SMS", "GPS", "PDF", "CEO", "VPN", "ATM", "DNA", "HIV",
}

# Persian technical vocabulary that a general reader plausibly does not know.
# This is the layer the Latin-only regex could never see: the jargon in a
# Persian post is usually IN PERSIAN.
FA_JARGON = {
    "ناترازی": "نبود تعادل میان تولید و مصرف (مثلاً در برق یا بودجه)",
    "تورم نقطه‌به‌نقطه": "مقایسه قیمت‌ها با همین ماه در سال گذشته",
    "تورم انتظاری": "تورمی که مردم و بازار پیش‌بینی می‌کنند و بر رفتارشان اثر می‌گذارد",
    "ضریب جینی": "شاخصی برای سنجش نابرابری درآمد؛ هرچه بالاتر، نابرابرتر",
    "سرمایه انسانی": "مجموع مهارت، دانش و سلامت نیروی کار یک کشور",
    "بهره‌وری": "میزان تولید به‌دست‌آمده از هر واحد نیروی کار یا سرمایه",
    "نرخ مشارکت": "سهم جمعیت آماده‌به‌کار که واقعاً در بازار کار حاضرند",
    "خام‌فروشی": "فروش مواد اولیه بدون فرآوری و ارزش‌افزوده",
    "شکاف دستمزد": "تفاضل میان دستمزد دو گروه (مثلاً زنان و مردان) برای کار مشابه",
    "اقتصاد مرزی": "داد و ستد رسمی و غیررسمی در نوار مرزی کشور",
    "تنش آبی": "شرایطی که برداشت آب از منابع تجدیدپذیر فراتر می‌رود",
    "فرونشست": "نشست تدریجی زمین به‌دلیل برداشت بی‌رویه آب زیرزمینی",
}


def _strip_glossary_block(post_fa: str) -> str:
    """Remove an already-appended glossary block.

    ROOT CAUSE this fixes: the saved post file has the glossary appended, so
    re-running extraction over that text re-ingests its own output -- "• KC:"
    appears without its "-135" model number and is mistaken for a bare acronym."""
    return re.split(r"\n+\s*📚", post_fa)[0]


def extract_candidate_terms(post_fa: str, max_terms: int = 6) -> list[str]:
    """Mechanical candidate extraction.

    ROOT CAUSES this fixes:
    (1) `\\b[A-Z]{2,6}\\b` matched the "KC" of "KC-135" and glossed an aircraft
        model number as if it were jargon.
    (2) Well-known acronyms (GDP/IMF/NATO) reached the LLM and wasted the slot.
    (3) The Latin-term regex can essentially never fire on a Persian post, so
        genuine Persian jargon was invisible. Persian terms are now candidates.
    (4) An already-appended glossary block fed its own terms back in.
    """
    post_fa = _strip_glossary_block(post_fa)
    acronyms = {t for t in ACRONYM_RE.findall(post_fa)
                if t.upper() not in WELL_KNOWN}
    latin = {t for t in FOREIGN_TERM_RE.findall(post_fa)
             if t.upper() not in WELL_KNOWN and len(t) > 3}
    stop = {"the", "and", "for", "with", "from", "of", "in", "to", "on", "by",
            "per", "capita"}
    latin = {t for t in latin if t.lower() not in stop}
    fa_hits = {term for term in FA_JARGON if term in post_fa}
    # FIGURED INDICATORS FIRST (live P22: 'شاخص بهای مصرف‌کننده ۴۰۳۰٫۳' had a
    # figure but no entry -- candidates only knew acronyms + FA_JARGON). Any
    # registry Persian label present in the post is a candidate, and ones with
    # a digit run nearby jump the queue: a figured term the reader can't parse
    # is the exact case the glossary exists for.
    import sqlite3 as _sq3
    import os as _os3
    _figured, _plain = [], []
    try:
        _dbp = _os3.path.join(_os3.path.dirname(_os3.path.abspath(__file__)),
                            "..", "..", "nexus_think_tank.db")
        _cx = _sq3.connect(_os3.path.normpath(_dbp))
        for _r in _cx.execute("SELECT label_fa FROM metric_registry WHERE label_fa IS NOT NULL"):
            _lab = (_r[0] or "").strip()
            # PARENTHESES-STRIPPED match (live P26): the registry label
            # 'تولید ناخالص داخلی (دلاری)' never appears verbatim -- posts say
            # 'تولید ناخالص داخلی'. The base phrase without (...) is the match
            # key; the full label stays the glossary term.
            _base = re.sub(r"\s*\([^)]*\)\s*", "", _lab).strip()
            _hit = None
            if len(_lab) >= 4 and _lab in post_fa:
                _hit = _lab
            elif len(_base) >= 4 and _base in post_fa:
                _hit = _base
            if _hit:
                _i = post_fa.find(_hit)
                _near = post_fa[max(0, _i - 40):_i + len(_hit) + 40]
                (_figured if re.search(r"[0-9۰-۹]", _near) else _plain).append(_lab)
        _cx.close()
    except Exception:
        pass
    out = _figured + sorted(acronyms) + sorted(latin) + sorted(fa_hits) + _plain
    # de-dupe preserving order
    seen, uniq = set(), []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq[:max_terms]


def make_glossary(post_fa: str, client=None, pace: float = 12.0) -> list[dict]:
    """Return <=3 entries [{term, fa}] for terms unfamiliar to a general
    Persian reader. Empty on any failure -- never blocks publishing.

    Curated Persian jargon is defined LOCALLY (deterministic, no LLM); only
    unknown acronyms/Latin terms are sent to the model. Previously every term
    went through the LLM, which rejected even NEET -- the exact case the feature
    exists for -- and shipped nothing."""
    try:
        cands = extract_candidate_terms(post_fa)
        if not cands:
            return []
        # Acronyms are the most opaque to a general reader (NEET, CPI...), so
        # they claim slots BEFORE curated Persian terms -- otherwise three
        # Persian entries fill the cap and NEET, the motivating case, drops out.
        # registry-defined terms resolve LOCALLY (deterministic, free).
        # They arrive first in cands (figured ones at the very front), so the
        # cap naturally keeps the terms that carry numbers.
        import sqlite3 as _sq4
        import os as _os4
        _reg = {}
        try:
            _dbp4 = _os4.path.join(_os4.path.dirname(_os4.path.abspath(__file__)),
                                 "..", "..", "nexus_think_tank.db")
            _cx4 = _sq4.connect(_os4.path.normpath(_dbp4))
            for _r in _cx4.execute("SELECT label_fa, gloss_fa FROM metric_registry WHERE gloss_fa IS NOT NULL"):
                _reg[(_r[0] or "").strip()] = _r[1]
            _cx4.close()
        except Exception:
            pass
        reg_known = [{"term": t, "fa": _reg[t]} for t in cands if t in _reg]
        rest = [t for t in cands if t not in _reg]
        unknown = [t for t in rest if t not in FA_JARGON]
        # LATIN DEBRIS FILTER (2026-09-05): paper titles in the outline leak
        # common English words (linkages, fluctuations) into candidates, and
        # the LLM happily "defines" them as glossary headwords. Only acronyms
        # (CPI, NEET) and alphanumeric codes genuinely need entries.
        # Case-insensitive (2026-09-05 r10: "Interactions" slipped through).
        import re as _re9
        unknown = [t for t in unknown
                   if not _re9.fullmatch(r"[a-z]+", (t or "").lower())]
        fa_known = [{"term": t, "fa": FA_JARGON[t]} for t in rest if t in FA_JARGON]
        out = []
        if not unknown:
            return (reg_known + fa_known)[:4]
        if client is None:
            from openai import OpenAI
            import os
            client = OpenAI(
                base_url=os.environ.get("ROUTER_BASE_URL",
                                        "http://localhost:20128/v1"),
                api_key=os.environ.get("ROUTER_API_KEY", "sk-x"))
        if pace:
            import time as _t
            _t.sleep(pace)
        prompt = (
            "این اصطلاحات در یک پست تحلیلی فارسی آمده‌اند: "
            + "، ".join(unknown)
            + ".\nبرای هر اصطلاحی که خواننده عام فارسی‌زبان معنایش را نمی‌داند، "
            "یک توضیح یک‌جمله‌ای ساده بنویس. اصطلاحات بسیار شناخته‌شده را رد کن. "
            "اگر یک اصطلاح فقط نام مدل تجهیزات یا کد فنی است (مثل KC-135)، آن را رد کن.\n"
            "حداکثر ۳ مورد. پاسخ فقط JSON:\n"
            '[{"term": "...", "fa": "توضیح یک‌جمله‌ای ساده فارسی"}]\n'
            "اگر هیچ موردی نیست: []\n\nمتن پست:\n" + post_fa[:2000])
        resp = _llm_with_retry(client, [
            {"role": "system", "content":
                "تو واژه‌نامه‌دار یک کانال تحلیلی فارسی هستی. فقط JSON بده."},
            {"role": "user", "content": prompt},
        ], 0.3, pace=0)
        raw = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\[.*\]", raw, re.S)
        if m:
            import json as _j
            items = _j.loads(m.group(0))
            for it in items:
                if len(out) >= 3:
                    break
                if isinstance(it, dict) and it.get("term") and it.get("fa"):
                    term = str(it["term"])[:40]
                    # guard: model must not invent terms absent from the post
                    if term and term in post_fa:
                        out.append({"term": term,
                                    "fa": str(it["fa"])[:200]})
        # deterministic entries FIRST (registry glosses, then curated
        # jargon) so figured terms survive the cap even when the LLM returns
        # its own picks; LLM novelties fill only leftover slots.
        seed = []
        for e in list(reg_known) + list(fa_known):
            if e["term"] not in {o["term"] for o in out + seed}:
                seed.append(e)
        out = (seed + out)[:4]
        return out
    except Exception:
        return []


def make_citations(problem_id: int) -> list[dict]:
    """Attribution block for the post: peer-reviewed studies behind its
    findings (title + year) and outlets behind its news claims.

    The desk cited 'پژوهش‌ها نشان داده‌اند' with zero attribution (live P22:
    synthetic-control sanctions paper, unnamed). Mechanical, from the problem's
    own evidence links -- never invented, capped at 3+2.
    Returns [{kind, text}]; empty on failure, never blocks publishing.
    """
    out = []
    try:
        import sqlite3 as _sq5
        import os as _os5
        _dbp5 = _os5.path.join(_os5.path.dirname(_os5.path.abspath(__file__)),
                             "..", "..", "nexus_think_tank.db")
        _cx5 = _sq5.connect(_os5.path.normpath(_dbp5))
        _cx5.row_factory = _sq5.Row
        seen = set()
        for r in _cx5.execute("""
            SELECT DISTINCT s.title, s.year FROM findings f
            JOIN studies s ON s.id = f.study_id
            JOIN question_evidence qe ON qe.evidence_type='finding'
                AND qe.evidence_id = f.id
            JOIN investigation_questions q ON q.id = qe.question_id
            WHERE q.problem_id=? ORDER BY s.year DESC""", (problem_id,)):
            t = (r["title"] or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append({"kind": "study",
                            "text": f"{t} ({r['year'] or 'بی‌تاریخ'})"})
            if len([o for o in out if o["kind"] == "study"]) >= 3:
                break
        for r in _cx5.execute("""
            SELECT DISTINCT a.original_url FROM claims cl
            JOIN source_artifacts a ON a.id = cl.artifact_id
            JOIN question_evidence qe ON qe.evidence_type='claim'
                AND qe.evidence_id = cl.id
            JOIN investigation_questions q ON q.id = qe.question_id
            WHERE q.problem_id=?""", (problem_id,)):
            u = (r["original_url"] or "").strip()
            m = re.search(r"https?://(?:www\.)?([^/]+)", u)
            host = m.group(1) if m else u[:40]
            if host and host not in seen:
                seen.add(host)
                out.append({"kind": "outlet", "text": f"گزارش {host}"})
            if len([o for o in out if o["kind"] == "outlet"]) >= 2:
                break
        _cx5.close()
    except Exception:
        pass
    return out


def make_tldr(post_fa: str, client=None, pace: float = 12.0) -> str:
    """One plain-Persian sentence (max ~140 chars) a newcomer understands.
    Cheap single call; on any failure returns '' and the post ships without."""
    try:
        if client is None:
            from openai import OpenAI
            import os
            client = OpenAI(
                base_url=os.environ.get("ROUTER_BASE_URL",
                                        "http://localhost:20128/v1"),
                api_key=os.environ.get("ROUTER_API_KEY", "sk-x"))
        if pace:
            import time as _t
            _t.sleep(pace)
        resp = _llm_with_retry(client, [
            {"role": "system", "content":
                "تو یک خلاصه‌ساز فارسی‌زبان هستی."},
            {"role": "user", "content":
                "این پست را در ONE جمله ساده برای کسی که هیچ پیش‌زمینه‌ای "
                "ندارد خلاصه کن. حداکثر ۲۰ کلمه، بدون اصطلاح تخصصی، بدون "
                "اموجی، فقط خود جمله:\n\n" + post_fa[:3000]},
        ], 0.4, pace=0)
        txt = (resp.choices[0].message.content or "").strip().strip('"')
        return txt[:200]
    except Exception:
        return ""


def _last_gate_detail() -> dict:
    """Last compose's gate failures for log diagnosability (run_v03 prints)."""
    try:
        return dict(globals().get("_GATE_DETAIL") or {})
    except Exception:
        return {}


def compose_v03(db, problem_id: int, composer_model: str | None = None,
                pace: float = 12.0) -> dict:
    """V0.3 pipeline: rich context -> free outline -> free write -> silent
    mechanical verification. Max one revision, only on mechanical failure."""
    from src.investigation_layer.writer import (
        build_context_pack, outline_post, write_post,
    )
    from src.investigation_layer.composer import build_dossier_and_text
    from src.investigation_layer.relevance import resolve_case, format_analysis_block
    from src.investigation_layer.ledger import avoid_block, record as ledger_record

    dossier_text, d = build_dossier_and_text(db, problem_id)
    if not d["coverage"]["has_evidence"]:
        return {"ok": False, "reason": "no evidence", "problem_id": problem_id}
    # CROSS-ATTEMPT FEEDBACK (2026-09-05): run_v03 saves the previous
    # attempt's QC errors to var/qc_feedback_<pid>.txt; they join the dossier
    # so the next roll fixes KNOWN flaws instead of rolling fresh ones.
    import os as _os
    _fbp = f"var/qc_feedback_{problem_id}.txt"
    if _os.path.exists(_fbp):
        try:
            _fb = open(_fbp, encoding="utf-8").read().strip()[:1200]
            if _fb:
                dossier_text += ("\n\nیادداشت منتقد از پیش‌نویس قبلی همین پست "
                                 "(این ایرادها را تکرار نکن):\n" + _fb)
        except Exception:
            pass

    case = resolve_case(d)
    if case["case"] == "discard":
        return {"ok": False, "problem_id": problem_id,
                "discarded": True, "reason": case["reason"]}

    ablock = format_analysis_block(case["analysis_items"])
    if ablock:
        dossier_text = dossier_text + "\n\n" + ablock
    else:
        dossier_text = dossier_text + (
            "\n\nOWN FORECAST (پیش‌بینی خودمان): بر اساس همین داده‌ها و روندشان، "
            "یک انتظار کوتاه‌مدت منطقی بنویس؛ عدد جدید ممنوع.")

    # V3: what changed since we last covered this problem? Injecting this makes
    # the desk CUMULATIVE -- the writer can say "six months ago X, today Y"
    # instead of treating every post as a first look. Facts only, no prose.
    try:
        import importlib.util as _ilu, os as _o, sqlite3 as _s3
        _tp = _o.path.join(_o.path.dirname(_o.path.dirname(_o.path.dirname(
            _o.path.abspath(__file__)))), "eval", "tracking.py")
        if _o.path.exists(_tp):
            _spec = _ilu.spec_from_file_location("tracking", _tp)
            _t = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_t)
            _c = _s3.connect("nexus_think_tank.db")
            _t.init(_c)
            _deltas = _t.changes(_c, problem_id)
            _c.close()
            _lines = _t.describe_changes(_deltas) if _deltas else []
            if _lines:
                dossier_text += ("\n\nتغییرات از آخرین بررسی ما (فقط واقعیت‌ها؛ "
                                 "اگر مرتبط است در روایت استفاده کن):\n"
                                 + "\n".join(f"- {x}" for x in _lines))
    except Exception:
        pass

    # V4: cross-problem links + our own standing forecasts. Injecting these lets
    # a post say something a single-problem pipeline cannot ("the same water
    # constraint caps the export target"), and lets the desk reference its own
    # dated predictions instead of pretending each post is a first opinion.
    try:
        import importlib.util as _ilu2, os as _o2, sqlite3 as _s32
        _sp = _o2.path.join(_o2.path.dirname(_o2.path.dirname(_o2.path.dirname(
            _o2.path.abspath(__file__)))), "eval", "synthesis.py")
        if _o2.path.exists(_sp):
            _spec2 = _ilu2.spec_from_file_location("synthesis", _sp)
            _sy = _ilu2.module_from_spec(_spec2)
            _spec2.loader.exec_module(_sy)
            _c2 = _s32.connect("nexus_think_tank.db")
            _c2.row_factory = _s32.Row
            _rel = [p for p in _sy.find_links(_c2)
                    if problem_id in (p["a"], p["b"])]
            _extra = []
            for p in _rel[:2]:
                other = p["b"] if p["a"] == problem_id else p["a"]
                _st = _c2.execute("SELECT statement FROM problems WHERE id=?",
                                  (other,)).fetchone()
                if _st:
                    _extra.append(
                        f"- مسئله مرتبط (اشتراک: {'، '.join(p['specific'])}): "
                        f"{_st['statement'][:180]}")
            _fc = _c2.execute(
                """SELECT prediction, target_horizon FROM forecasts
                   WHERE evaluation_status='unresolved'
                     AND conditions LIKE ? ORDER BY id DESC LIMIT 1""",
                (f"%problem_id={problem_id};%",)).fetchone()
            _c2.close()
            if _extra:
                dossier_text += ("\n\nپیوندهای بین‌مسئله‌ای (اگر واقعاً به روایت "
                                 "کمک می‌کند استفاده کن؛ اجبار نیست):\n"
                                 + "\n".join(_extra))
            if _fc:
                dossier_text += (
                    f"\n\nپیش‌بینی ثبت‌شده خودمان (تا {_fc['target_horizon']}): "
                    f"{_fc['prediction']}\nاگر داده تازه آن را تأیید یا نقض "
                    f"می‌کند، صادقانه اشاره کن.")
    except Exception:
        pass

    # V5: peer-reviewed mechanisms. Papers explain WHY something happens, which
    # macro series and news cannot; they are also the natural source of the
    # specialised vocabulary the glossary exists to unpack. Attribution is
    # carried through so the writer cites rather than asserts.
    try:
        import importlib.util as _ilu3, os as _o3, sqlite3 as _s33
        _pp = _o3.path.join(_o3.path.dirname(_o3.path.dirname(_o3.path.dirname(
            _o3.path.abspath(__file__)))), "eval", "papers.py")
        if _o3.path.exists(_pp):
            _spec3 = _ilu3.spec_from_file_location("papers", _pp)
            _pa = _ilu3.module_from_spec(_spec3)
            _spec3.loader.exec_module(_pa)
            _c3 = _s33.connect("nexus_think_tank.db")
            _mech = _pa.mechanisms(_c3, problem_id)
            _c3.close()
            _blk = _pa.mechanism_block(_mech)
            if _blk:
                dossier_text += "\n\n" + _blk
    except Exception:
        pass

    # A4 evidence-level anti-repetition: tell the writer which indicators the
    # desk's recent posts already headlined, so new posts lead with NEW
    # evidence instead of the same macro numbers (user: generic-feeling desk).
    try:
        import sys as _sys4, os as _o4
        _ep = _o4.path.join(_o4.path.dirname(_o4.path.dirname(_o4.path.dirname(
            _o4.path.abspath(__file__)))), "eval")
        if _ep not in _sys4.path:
            _sys4.path.insert(0, _ep)
        from evidence_memory import context_note as _cnote
        _note = _cnote(problem_id)
        if _note:
            dossier_text += "\n\n" + _note
    except Exception:
        pass

    pack = build_context_pack(dossier_text)

    # Ledger gives recent openings so the writer avoids them by phrase.
    try:
        import json as _json, os as _os
        _lp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                            "..", "..", "var", "editorial_ledger.json")
        recent = []
        if _os.path.exists(_lp):
            with open(_lp, encoding="utf-8") as f:
                recent = [r.get("opening", "") for r in _json.load(f)]
    except Exception:
        recent = []

    model = composer_model or None
    outl = outline_post(pack, model=model, pace=pace,
                        problem_id=problem_id, recent_openings=recent)
    post = write_post(pack, outl, recent, model=model, pace=pace)
    post = post.strip().strip("`").strip()
    if post.startswith("```"):
        post = post.split("\n", 1)[-1]

    # Silent mechanical verification only.
    guard = verify_numbers(post, dossier_text)
    san = sanity_check(post, dossier_text)
    global _GATE_DETAIL
    try:
        _GATE_DETAIL = {
            "unmatched": (guard.get("unmatched") or [])[:6],
            "violations": ((san or {}).get("violations") or [])[:4],
        }
    except Exception:
        pass
    # Explanation gate: a causal claim with no mechanism leaves the reader's
    # biggest question unanswered (a post once said CO2 hinders exports and never
    # said how). Mechanical, so it does not depend on the writer obeying a rule.
    from src.investigation_layer.writer import unexplained_claims
    unexp = unexplained_claims(post)
    # Prescription gate (Phase C): policy advice naming no verified lever.
    # Lever names parsed from the dossier text the composer itself saw.
    import re as _re2
    _lever_sec = _re2.search(r"VERIFIED INTERVENTION LEVERS:\n((?:  - .*\n)+)",
                             dossier_text or "")
    _levers = (_re2.findall(r"  - (.*?) \(Iran relevance:",
                            _lever_sec.group(1)) if _lever_sec else [])
    from src.investigation_layer.writer import prescription_without_lever as _pwl
    presc = _pwl(post, _levers)
    attempts = [{"outline": outl.get("angle", ""), "guard_ok": guard.get("ok"),
                 "sanity_violations": len((san or {}).get("violations") or []),
                 "unexplained": len(unexp), "prescriptions": len(presc)}]

    if not guard.get("ok") or ((san or {}).get("violations")) or unexp or presc:
        # one revision round, feedback is purely mechanical facts
        fb = []
        if not guard.get("ok"):
            fb.append("این اعداد در مواد نیستند و باید حذف یا اصلاح شوند: "
                      + ", ".join(map(str, guard.get("unmatched") or [])))
        for v in (san or {}).get("violations") or []:
            fb.append(f"تناقض زمانی/روند: {v}")
        for u in unexp[:3]:
            fb.append("این ادعای علّی سازوکار ندارد؛ یا در یک-دو جمله توضیح بده "
                      "«از چه راهی» این اثر رخ می‌دهد، یا کل ادعا را حذف کن: "
                      f"«{u}»")
        for p in presc[:3]:
            fb.append("این جمله توصیهٔ سیاستی می‌کند اما هیچ‌یک از اهرم‌های بخش "
                      "«VERIFIED INTERVENTION LEVERS» را نام نمی‌برد؛ یا اهرم مرتبط "
                      "را با اثر اندازه‌گیری‌شده‌اش نام ببر، یا توصیه را حذف کن: "
                      f"«{p}»")
        rev_usr = ("نسخه قبلی:\n---\n" + post + "\n---\n\nفقط این مشکلات مکانیکی را رفع کن و کل پست را دوباره بده:\n"
                   + "\n".join(fb))
        from src.route_health import chat as _rchat_rev
        post2, _used = _rchat_rev("compose",
            messages=[{"role": "user", "content":
                       f"مواد خام:\n\n{pack[:14000]}\n\n{rev_usr}"}],
            temperature=0.8, model=model)
        post2 = (post2 or "").strip()
        g2 = verify_numbers(post2, dossier_text)
        s2 = sanity_check(post2, dossier_text)
        u2 = unexplained_claims(post2)
        pr2 = _pwl(post2, _levers)
        attempts.append({"revision": True, "guard_ok": g2.get("ok"),
                         "sanity_violations": len((s2 or {}).get("violations") or []),
                         "unexplained": len(u2), "prescriptions": len(pr2)})
        # accept the revision when it is no worse mechanically and explains more
        if g2.get("ok") and not (s2 or {}).get("violations") and len(u2) <= len(unexp) and len(pr2) <= len(presc):
            post, guard, san, unexp, presc = post2, g2, s2, u2, pr2

    ok_mech = bool(guard.get("ok")) and not ((san or {}).get("violations"))
    if not ok_mech:
        return {"ok": False, "problem_id": problem_id,
                "reason": "mechanical gates failed after revision",
                "attempts": attempts, "post_fa": post}

    # P2 deterministic Persian normalisation: fix unjoined «می», western digits,
    # punctuation spacing AFTER all revision rounds (so gates judge the text as
    # it will ship) and BEFORE QC/number-verify re-checks would matter — the
    # normalizer never touches digit VALUES, only their script (0->۰), which the
    # verifier already normalises.
    from src.investigation_layer.fa_norm import normalise_fa, source_fa
    from src.investigation_layer.fa_norm import head_key as _hk
    # Metric->provider map for deterministic placeholder repair (2026-09-05
    # r11: the model emits "[بنا بر منبع X]" under attribution pressure).
    # Sourced from the FLAT measurements list (2026-09-05 r14: series points
    # are rebuilt as {period, value} WITHOUT source -- the map was empty and
    # no cures ran at all).
    _mmap: dict = {}
    _full: dict = {}  # full fa names only (r19 missing-source insertion)
    try:
        _ind_src: dict = {}
        for _m in (d.get("measurements") or []):
            if _m.get("source"):
                _ind_src.setdefault(_m.get("indicator", ""), _m["source"])
        for _sk, _ser in (d.get("measurement_series") or {}).items():
            _nm = (_ser.get("fa_name") or _sk or "").strip()
            _src = _ind_src.get(_ser.get("indicator", ""))
            if _nm and _src:
                _mmap[_nm] = source_fa(_src)
                _full[_nm] = source_fa(_src)
                # Head key, NOT single words (r21: "دلار" collided across
                # metrics and the corrector checked Brent against the wrong
                # provider, skipping the fix).
                _h = _hk(_nm)
                if _h:
                    _mmap.setdefault(_h, source_fa(_src))
    except Exception:
        pass
    post = normalise_fa(post, _mmap or None, _full or None)
    try:
        from src.investigation_layer.fa_norm import LAST_STATS as _ls
        print(f"  [cures] map={len(_mmap)} full={len(_full)} "
              + " ".join(f"{k}={_ls.get(k, 0)}" for k in
                         ("scaffold", "placeholders", "labels", "rounded",
                          "inserted", "skip_prov", "skip_nonum", "defparens",
                          "defappos", "deduped", "yehfix", "dotfix",
                          "splits")))
    except Exception:
        pass

    # QC is a proofread pass, not a veto: fix its findings with one targeted
    # repair call (V0.3). Only unfixable QC failures block publication.
    qc = qc_post(post, dossier_text, pace=pace)
    # KEYSTONE GUARD (2026-09-05 r18): every non-year number in the scenario
    # spec must appear in the post (r17 shipped without the 56.4 keystone,
    # twice uncaught). Mechanical miss -> persian_errors so the retry loop +
    # feedback file carry it to the next attempt.
    # TRIGGER GUARD (r22): the scenario اگر must open in paragraph 1.
    try:
        from src.investigation_layer.fa_norm import keystone_missing as _km
        _miss = _km(post, d.get("scenario") or "")
        if _miss:
            qc.setdefault("persian_errors", []).extend(
                f"عدد کلیدی {m} از صورت سناریو در متن نیست؛ "
                f"با منبع و سال کنارش بیاور" for m in _miss)
            qc["qc_pass"] = False
        if d.get("scenario"):
            # r25: split("\n\n") failed vacuously -- the composer emits
            # single newlines, so the whole post looked like "paragraph 1".
            # First non-empty LINE is the opener (files store one para/line).
            _lines = [l for l in (post or "").splitlines() if l.strip()]
            _head = _lines[0] if _lines else ""
            if len(_head) < 500 and "اگر" not in _head:
                qc.setdefault("persian_errors", []).append(
                    "ماشهٔ سناریو (اگر...) باید در پاراگراف اول باشد، "
                    "نه پاراگراف‌های بعد")
                qc["qc_pass"] = False
    except Exception:
        pass
    if not qc.get("qc_pass"):
        try:
            from src.route_health import chat as _rchat_rep
            fixes = "; ".join(map(str, (qc.get("persian_errors") or [])[:3]))
            fixed, _used = _rchat_rep("compose",
                messages=[{"role": "user", "content":
                           ("پست زیر را فقط از نظر خطاهای نگارشی گزارش‌شده اصلاح کن "
                            "و کل متن اصلاح‌شده را بده. محتوا را تغییر نده.\n"
                            f"خطاها: {fixes}\n---\n{post}")}],
                temperature=0.4, model=model)
            fixed = (fixed or "").strip()
            if fixed:
                post = fixed
                # the repair LLM re-emits Latin digits/pacing: renormalise the
                # FINAL text (live P18 shipped '20,914.3' in Latin script).
                post = normalise_fa(post, _mmap or None, _full or None)
                qc = qc_post(post, dossier_text, pace=pace)
                # KEYSTONE GUARD, post-repair (r18: the re-QC above overwrote
                # the pre-repair guard's miss, shipping without 56.4 again).
                try:
                    from src.investigation_layer.fa_norm import (
                        keystone_missing as _km2)
                    _miss2 = _km2(post, d.get("scenario") or "")
                    if _miss2:
                        qc.setdefault("persian_errors", []).extend(
                            f"عدد کلیدی {m} از صورت سناریو در متن نیست؛ "
                            f"با منبع و سال کنارش بیاور" for m in _miss2)
                        qc["qc_pass"] = False
                    if d.get("scenario"):
                        _lines2 = [l for l in (post or "").splitlines()
                                   if l.strip()]
                        _head2 = _lines2[0] if _lines2 else ""
                        if len(_head2) < 500 and "اگر" not in _head2:
                            qc.setdefault("persian_errors", []).append(
                                "ماشهٔ سناریو (اگر...) باید در پاراگراف "
                                "اول باشد، نه پاراگراف‌های بعد")
                            qc["qc_pass"] = False
                except Exception:
                    pass
        except Exception:
            pass
    try:
        from src.investigation_layer.fa_norm import LAST_STATS as _ls2
        print(f"  [cures-final] "
              + " ".join(f"{k}={_ls2.get(k, 0)}" for k in
                         ("scaffold", "placeholders", "labels", "rounded",
                          "inserted", "skip_prov", "skip_nonum", "defparens",
                          "defappos", "deduped", "yehfix", "dotfix",
                          "splits")))
    except Exception:
        pass
    if not qc.get("qc_pass"):
        # publishable-with-note: surface the failure but keep the artifact;
        # a one-character typo must not destroy an otherwise sound post.
        qc["published_with_note"] = True

    # Short reviewer note (not scored, not gating).
    note = ""
    try:
        from src.route_health import chat as _rchat_note
        note, _used = _rchat_note("fast",
            messages=[{"role": "system", "content":
                       "ویراستار فارسیزبان هستی. در یک جمله حداکثر، مهم‌ترین نقطه قوت و مهم‌ترین ضعف این پست را بگو. بدون امتیاز."},
                      {"role": "user", "content": post[:3000]}],
            temperature=0.5, model=model)
        note = (note or "").strip()[:400]
    except Exception:
        pass

    try:
        opening = post.strip().split("\n")[0][:120]
        ledger_record(format_name="v03", hook_summary=outl.get("angle", "")[:160],
                      insight_kind="", opening_fa=opening, problem_id=problem_id)
    except Exception:
        pass

    return {"ok": True, "problem_id": problem_id, "pipeline": "v03",
            "angle": outl.get("angle", ""), "outline": outl.get("outline", []),
            "hook_strategy": outl.get("hook_strategy", ""),
            "post_fa": post, "rounds": len(attempts), "reviewer_note": note,
            "qc": qc, "attempts": attempts, "format": "v03",
            "tldr": make_tldr(post, pace=pace),
            "glossary": make_glossary(post, pace=pace),
            "citations": make_citations(problem_id),
            "final_review": {"note": note}, "dossier": d}
