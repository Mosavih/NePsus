"""V0.3 -- composer rewrite: reader-first contract + outline-then-write.

Replaces the 13-rule prompt regime. Philosophy (from the vision reset):
quality systems invest in inputs and structure, and leave prose nearly free.

Two cheap calls:
  A. outline_post(): a 4-line Persian narrative skeleton chosen freely from
     the context pack (hook / turn / evidence beat / landing).
  B. write_post(): writes ONLY from its own outline under a ~4-sentence
     reader-first contract.

No format taxonomy, no char window, no emoji quota, no CASE bureaucracy.
Mechanical gates still verify AFTER writing (see reviewer.py) but do not
shape the prompt.
"""
from __future__ import annotations

import json
import re
import time as _time

from src.investigation_layer.reviewer import _client

DEFAULT_MODEL = "compose"  # task key — resolved to a combo via models.combo_for

# --- Call A: outline -------------------------------------------------------

OUTLINE_SYS = (
    "You are a sharp Persian-language editorial writer for a Telegram channel "
    "about Iran's real-world problems, read by curious general readers. You "
    "plan pieces, you don't write them yet. Respond ONLY with JSON."
)

# Hook strategies. ROOT CAUSE this addresses: the outline call asked only "why
# should a curious Iranian read RIGHT NOW?", and because every context pack opens
# with news, the model kept reaching for the same time-pressure framing --
# «در روزهایی که...» (4 posts) and «چرا با وجود...» (4 posts). Telling the writer
# "don't start like recent posts" is a prohibition, and prohibitions do not
# create variety. Assigning a POSITIVE, rotating structural strategy does.
HOOK_STRATEGIES = [
    ("مقایسه", "با یک مقایسه شروع کن: ایران در برابر یک کشور/دوره دیگر، یا "
               "وعده رسمی در برابر عدد واقعی. هیچ اشاره‌ای به اخبار روز نکن."),
    ("صحنه", "با یک صحنه انسانی مشخص شروع کن: یک نفر، یک خانه، یک صف، یک "
             "کارگاه. از کلیات و آمار در جمله اول پرهیز کن."),
    ("عدد غافلگیرکننده", "با یک عدد غافلگیرکننده از مواد خام شروع کن و "
                          "بلافاصله بگو چرا باورکردنی نیست."),
    ("پرسش ساده", "با یک پرسش کوتاه و ساده شروع کن که خواننده عادی هم "
                   "بپرسد؛ بدون «چرا با وجود...» و بدون فهرست کردن شرایط."),
    ("تاریخ", "با یک نقطه در گذشته شروع کن (یک دهه پیش، سال مشخص) و مسیر "
              "رسیدن به امروز را باز کن."),
    ("تضاد ساختاری", "با یک تضاد ساختاری شروع کن: چیزی که روی کاغذ هست و در "
                      "واقعیت نیست. بدون ارجاع به تیتر رسانه‌ها."),
]

BANNED_OPENERS = ["در روزهایی که", "چرا با وجود", "در شرایطی که",
                  "این روزها که", "در حالی که خبرها", "تیتر یک رسانه"]


def pick_hook_strategy(problem_id: int, recent_openings: list[str]) -> tuple:
    """Rotate hook strategy, preferring one whose shape is NOT already visible in
    recent openings. Rotation is seeded by problem_id so reruns of the same
    problem stay stable, but a strategy is skipped when the last few openings
    already show its signature (e.g. don't assign 'پرسش ساده' when the last
    openings are all questions)."""
    n = len(HOOK_STRATEGIES)
    recent = " ".join(recent_openings[-4:])
    # crude signature detection per strategy
    sig = {
        "پرسش ساده": ("چرا", "؟"),
        "تاریخ": ("دهه", "سال ۱"),
        "عدد غافلگیرکننده": ("درصد",),
    }
    order = [(problem_id + i) % n for i in range(n)]
    for idx in order:
        name, rule = HOOK_STRATEGIES[idx]
        marks = sig.get(name)
        if marks and sum(recent.count(mk) for mk in marks) >= 3:
            continue  # this shape is saturated in recent openings
        return name, rule
    return HOOK_STRATEGIES[problem_id % n]


OUTLINE_USR = """مواد خام برای یک پست تلگرام درباره یک مسئله واقعی ایران:

{context}

یک طرح روایت ۴خطی به فارسی بساز:
- قلاب: {hook_rule}
- چرخش: نکته غیربدیهی یا تنشی که روایت را جلو می‌برد
- ضربه شواهد: کدام اعداد/یافته‌ها (دقیقاً از مواد بالا) این چرخش را نشان می‌دهند
  و سازوکار هر کدام چیست — یعنی «از چه راهی» آن عدد به نتیجه وصل می‌شود
- فرود: خواننده با چه فهم تازه‌ای پست را ترک می‌کند

قاعده مهم: فقط عواملی را در طرح بیاور که سازوکارشان را از مواد بالا می‌توان
توضیح داد. عاملی که نمی‌توانی توضیح دهی چرا اثر می‌گذارد، در طرح نیاور.

این عبارت‌ها در قلاب ممنوع است (بیش از حد استفاده شده‌اند): {banned}

زاویه را آزاد انتخاب کن؛ فقط به مواد پایبند باش. اگر چند زاویه ممکن است،
جالب‌ترینش را بردار — نه امن‌ترین.

فقط JSON:
{{"angle": "یک جمله توصیف زاویه",
  "outline": ["قلاب: ...", "چرخش: ...", "شواهد: ...", "فرود: ..."]}}"""


def outline_post(context_pack: str, model: str | None = None,
                 pace: float = 12.0, problem_id: int = 0,
                 recent_openings: list[str] | None = None) -> dict:
    from src.route_health import chat as _rchat
    if pace:
        _time.sleep(pace)
    name, rule = pick_hook_strategy(problem_id, recent_openings or [])
    raw, _used = _rchat("compose",
        messages=[{"role": "system", "content": OUTLINE_SYS},
                  {"role": "user", "content": OUTLINE_USR.format(
                      context=context_pack[:16000],
                      hook_rule=rule,
                      banned="، ".join(f"«{b}»" for b in BANNED_OPENERS))}],
        temperature=0.85, model=model)
    raw = (raw or "").strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return {"angle": "", "outline": [], "hook_strategy": name}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"angle": "", "outline": [], "hook_strategy": name}
    return {"angle": str(d.get("angle") or "")[:300],
            "outline": [str(x)[:300] for x in (d.get("outline") or [])[:4]],
            "hook_strategy": name}


# --- Call B: write ---------------------------------------------------------

WRITER_SYS = (
    "You are the same Persian editorial voice: warm, precise, curious. You "
    "write Telegram posts for general readers about Iran's real problems."
)

# The whole contract. Four sentences. Nothing else is imposed; mechanical
# gates verify evidence-fidelity afterwards instead of prompting for it.
def news_framing_score(text: str) -> int:
    """Count news-framing tells in a post's opening.

    Blocklisting exact phrases is whack-a-mole -- «در روزهایی که» was banned and
    the model produced «این روزها که ... تیتر یک رسانه‌هاست» instead. This
    measures the SHAPE (time-pressure + media reference) so convergence is
    detectable even when the wording is new."""
    head = (text or "")[:260]
    tells = ["روزهایی که", "این روزها", "تیتر یک", "رسانه‌ها", "خبرها",
             "اخبار", "در شرایطی که", "چرا با وجود"]
    return sum(1 for t in tells if t in head)


CAUSAL_VERBS = ["اثر می‌گذارد", "تأثیر می‌گذارد", "تاثیر می‌گذارد", "منجر می‌شود",
                "منجر به", "باعث", "موجب", "محدود می‌کند", "کاهش می‌دهد",
                "افزایش می‌دهد", "مانع", "سد راه", "گره خورده", "دامن می‌زند"]
EXPLAIN_MARKERS = ["چون", "زیرا", "به این دلیل", "از این راه", "یعنی", "چرا که",
                   "به دلیل", "در نتیجه", "بنابراین", "از آنجا که", "سازوکار",
                   "به عبارت دیگر", "که این", "وقتی", "هنگامی"]


def unexplained_claims(post_fa: str) -> list[str]:
    """Sentences asserting causation with no explanation nearby.

    ROOT CAUSE this detects: a post claimed CO2 emissions hinder agricultural
    exports and never said HOW, leaving the reader's biggest question unanswered
    -- while the chart was built on that very claim. A prompt rule alone would not
    catch it (adding rules per defect is exactly the constraint saturation that
    hurt earlier versions), so causal claims are now checked mechanically:
    a claim must carry an explanation marker in its own sentence or the next one."""
    import re as _re
    sents = [s.strip() for s in _re.split(r"(?<=[.!؟])\s+", post_fa or "")
             if s.strip()]
    flagged = []
    for i, s in enumerate(sents):
        if not any(v in s for v in CAUSAL_VERBS):
            continue
        window = s + " " + (sents[i + 1] if i + 1 < len(sents) else "")
        if not any(m in window for m in EXPLAIN_MARKERS):
            flagged.append(s[:150])
    return flagged


PRESCRIPTION_MARKERS = ("باید", "لازم است", "ضروری است", "پیشنهاد می‌شود",
                        "توصیه می‌شود", "می‌بایست")


def prescription_without_lever(post_fa: str, lever_names: list[str]) -> list[str]:
    """Sentences prescribing action while naming no evidence-backed lever.

    ROOT CAUSE this detects (Phase C): the interventions table once held
    radiation cameras and the Korea-US trade deal as 'solutions for Iran' --
    free-form policy advice grounded in nothing. Prescription is now allowed
    ONLY for levers in the dossier's VERIFIED INTERVENTION LEVERS section.
    Mechanical like unexplained_claims: fires on 2+ prescription markers with
    no lever name anywhere in the post (a single 'باید' is ordinary prose).
    """
    import re as _re
    post = post_fa or ""
    levers = [l for l in (lever_names or []) if l and l in post]
    if levers:
        return []
    sents = [s.strip() for s in _re.split(r"(?<=[.!؟])\s+", post) if s.strip()]
    hits = [s[:150] for s in sents
            if sum(s.count(m) for m in PRESCRIPTION_MARKERS) >= 1]
    return hits if len(hits) >= 2 else []


WRITER_CONTRACT = """پست تلگرام به فارسی بنویس.

قرارداد:
۱) برای خواننده ایرانی کنجکاو بنویس، نه برای کارشناس و نه برای سانسورچه.
۲) یک استدلال واحد بساز که به جای غیرمنتظره‌ای برسد؛ هر عدد باید در خدمت آن باشد.
۳) همه اعداد فقط از مواد بالا؛ هیچ عددی از دانش عمومی خودت نیاور. هر عدد را
   با واحدش بیاور (تُن، درصد، کیلووات‌ساعت...) — عدد بی‌واحد برای خواننده بی‌معناست.
۴) هیچ عدد یا عاملی را بدون توضیح سازوکارش رها نکن. اگر می‌گویی X روی Y اثر
   می‌گذارد، باید در همان‌جا بگویی «از چه راهی»: زنجیره علت را در یک یا دو جمله
   باز کن. اگر سازوکارش را از مواد بالا نمی‌دانی، آن عامل را حذف کن — آوردنِ
   عددی که توضیح ندارد، برای خواننده علامت سؤال می‌سازد.
۵) خواننده باید پست را بدون هیچ «چرا؟» بی‌پاسخ ترک کند. طول متن آزاد است؛
   ترجیح می‌دهیم کمی بلندتر باشد تا چیزی ناقص بماند. اما جمله‌های عریض:
   وقتی جمله‌ای از ~۳۰ کلمه گذشت، آن را با «و» یا «؛» به دو جملهٔ خواناتر بشکن —
   پست بلند با جمله‌های کوتاه، نه پست کوتاه با جمله‌های عریض.
۶) حداکثر ۳ ایموجی مرتبط، فقط در آغاز پاراگراف‌ها یا کنار آمار کلیدی؛ هرگز داخل جمله و هرگز تزئینی.
۷) جمله اول را با ارجاع به اخبار روز، تیتر رسانه‌ها یا «این روزها/در روزهایی که» شروع نکن.
   مستقیم به جان موضوع برو.
۸) مثل هیچ‌کدام از پست‌های اخیر شروع نکن:

{recent_openings}

۹) قالب یخ‌زدهٔ میز (مرجع: پست ۲۲): پاراگراف اول با لنگر تاریخیِ تاریخ‌دار باز شو
(«سال ۲۰۱۸ با خروج آمریکا از برجام...»)؛ پاراگراف دوم پژوهش‌محور («بررسی‌های علمی
نشان می‌دهند...») با یافتهٔ نامتوازن/غیربدیهی؛ اعداد کلان فقط در خدمت همان استدلال
بیایند؛ پایان‌بندی با ترکیب روبه‌جلو که بگوید هزینه بر دوش کیست. عددِ تنها، خاطرهٔ
تنها، پژوهشِ بی‌ارجاع — هر سه ممنوع.
۱۰) تقویم: اکنون سال ۲۰۲۶ است؛ سال ۲۰۲۵ تمام شده. عددِ سالِ تمام‌شده هرگز
«پیش‌بینی» نیست — اگر ارائه‌دهنده آن را قطعی نداده، بگو «برآورد». کلمهٔ
«پیش‌بینی» فقط برای سال‌های آینده و فقط با ذکر منبع پیش‌بینی‌کننده (مثلاً صندوق
بین‌المللی پول).
۱۱) سلسله‌مراتب تازگی (مهم): عددِ تیتر و جملهٔ اصلی باید از تازه‌ترین وینتج
موجود بیاید — مشاهده‌های ماهانهٔ ۲۰۲۶ بر سالانهٔ ۲۰۲۵ مقدم‌اند و سالانهٔ ۲۰۲۵
بر ۲۰۲۴. عدد سالانهٔ ۲۰۲۴ را هرگز به‌عنوان «اکنون» جا نزن وقتی عدد تازه‌تر هست؛
سری‌های سالانه نقش «مبدأ» (از کجا آمدیم) و عددهای تازه نقش «اکنون» را دارند.
اگر برای سنجهٔ اصلی هیچ عدد ۲۰۲۵+ نیست، همین نبود را یک‌جمله‌ای و صریح بگو.
۱۲) راهکار فقط از بخش «اهرم‌های تأییدشده» در مواد بالا؛ اگر آن بخش خالی است،
هیچ توصیهٔ سیاستی ننویس — تحلیلِ «چه شد و چرا» کافی است. نسخه‌پیچی بدون اهرم،
حذف می‌شود.

طرح روایت خودت:
{outline}

بنویس."""


def write_post(context_pack: str, outline: dict, recent_openings: list[str],
               model: str | None = None, pace: float = 12.0) -> str:
    from src.route_health import chat as _rchat
    if pace:
        _time.sleep(pace)
    openings = "\n".join(f"- {o}" for o in recent_openings[-6:]) or "- (نمونه‌ای ثبت نشده)"
    usr = WRITER_CONTRACT.format(recent_openings=openings,
                                 outline="\n".join(outline.get("outline") or []))
    # Context goes before the contract so the outline stays adjacent to the ask.
    full_usr = f"مواد خام:\n\n{context_pack[:15000]}\n\n{usr}"
    text, _used = _rchat("compose",
        messages=[{"role": "system", "content": WRITER_SYS},
                  {"role": "user", "content": full_usr}],
        temperature=0.9, model=model)
    return (text or "").strip()


# --- Context pack assembly --------------------------------------------------

def build_context_pack(dossier_text: str) -> str:
    """V0.3 keeps the dossier as-is minus bureaucratic headers. The CASE line
    and block titles are material, not instructions; strip only the former."""
    lines = dossier_text.splitlines()
    out = [ln for ln in lines
           if not ln.startswith("CASE:") and not ln.startswith("INSIGHT CANDIDATES")
           and not ln.startswith("EDITORIAL LEDGER")]
    return "\n".join(out).strip()
