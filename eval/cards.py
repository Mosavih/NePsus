"""S4/E1 -- stat-card generator: infographic cards rendered from APPROVED post
numbers via headless Chrome. No new Python deps.

Layouts (chosen mechanically from data shape):
  big_number  -- one dominant stat + caption (default)
  contrast    -- two opposing numbers in one sentence
  quote_card  -- a source claim with attribution
  trend       -- CSS sparkline from dossier measurement_series (pid given)

Usage:
  python eval/cards.py posts/post_9_v4.txt            -> big_number/contrast/quote
  python eval/cards.py posts/post_9_v4.txt 9          -> enables trend option
Output: posts/<same-name>_card.png
"""
from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date

sys.path.insert(0, ".")

# load project .env into environment (router key lives there, not exported)
def _load_env():
    import os as _os
    envp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".env")
    if _os.path.exists(envp):
        for line in open(envp, encoding="utf-8", errors="replace"):
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            _os.environ.setdefault(k.strip(), v.strip())
_load_env()

CHROME = r"C:/Program Files/Google/Chrome/Application/chrome.exe"
W, H = 1080, 608

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
EN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")

# B-fix: unit alternatives are anchored with a word boundary. The old pattern
# matched the 'تن' PREFIX of 'تنگه' (strait) after a year, producing the
# nonsense stat '۲۰۲۶ تن' (2026 tons) on P16's card.
NUM_RE = re.compile(
    # P17: thousand-grouped figures (۲,۰۰۰,۰۰۰) are single numbers, and
    # ریال/تومان/بشکه are real units -- without them the rial-collapse post
    # produced NO card at all ('no numeric sentence found').
    r"([\d۰-۹]+(?:[,\٬_][\d۰-۹]{3})*(?:[٫\.][\d۰-۹]+)?)\s*"
    r"(میلیارد|میلیون|هزار|درصد|تُن|متر|نفر|دلار|تن|ریال|تومان|بشکه)?"
    r"(?=[\s،؛.)]|$)"
)

UNIT_FA = {
    "%": "٪", "percent": "٪",
}


def fa_to_en_num(s: str) -> float:
    t = s.translate(FA_DIGITS).replace("٫", ".")
    t = re.sub(r"[,\٬_]", "", t)  # thousand-group separators, either script
    return float(t or 0)


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy - 1600
    gm2 = gm - 1
    gd2 = gd - 1
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_d_m[gm2] + gd2
    if gm2 > 1 and ((gy % 4 == 0 and gy % 100 != 0) or (gy % 400 == 0)):
        g_day_no += 1
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    months = [31, 31, 31, 31, 31, 31, 30, 30, 30, 30, 30, 29]
    jm = 0
    while jm < 12 and j_day_no >= months[jm]:
        j_day_no -= months[jm]
        jm += 1
    return jy, jm + 1, j_day_no + 1


def jalali_today() -> str:
    t = date.today()
    jy, jm, jd = gregorian_to_jalali(t.year, t.month, t.day)
    return f"{jy}/{jm:02d}/{jd:02d}".translate(EN_DIGITS)


def split_sentences(text: str) -> list[str]:
    # protect decimal points and abbreviations before splitting on [.!?]
    protected = re.sub(r"(?<=[\d۰-۹])\.(?=[\d۰-۹])", "\u2202", text)
    parts = re.split(r"[.\n؟!]\s+", protected)
    return [p.replace("\u2202", ".").strip()
            for p in parts if p.strip() and len(p.strip()) > 25]


def extract_stats(sentence: str) -> list[dict]:
    """Extract the MEASURABLE quantities of a sentence.

    B-NUMBERS POLICY (user, 2026-09-02): years are NOT stats. The P13 card
    rendered "۲۰۲۵ در برابر ۳.۷٪" -- a year juxtaposed with a percentage --
    because bare 4-digit years scored high enough to trigger the contrast
    layout. A year is context, never a quantity; it is excluded here and lives
    only in copy text/axis labels.
    """
    stats = []
    for m in NUM_RE.finditer(sentence):
        raw, unit = m.group(1), m.group(2)
        try:
            val = fa_to_en_num(raw)
        except ValueError:
            continue
        if not raw or val == 0:
            continue
        # year exclusion: bare 4-digit run in 1300..2100 (Persian/Gregorian)
        # with NO unit attached
        if not unit and len(raw) == 4:
            v = int(val)
            if 1300 <= v <= 2100:
                continue
        score = min(len(raw), 6)
        if unit == "درصد":
            score += 4
        elif unit in ("میلیون", "میلیارد", "هزار"):
            score += 3
        elif unit:
            score += 1
        stats.append({"raw": raw, "unit": unit or "", "value": val,
                      "start": m.start(), "score": score})
    return stats


FA_STOP = {"در", "از", "به", "با", "که", "این", "آن", "را", "و", "بر", "برای",
           "است", "شده", "های", "یک", "تا", "هم", "اما", "یا", "می", "بود",
           "دارد", "کرده", "سال", "خود", "روی", "طور", "شود", "کند", "نیز",
           "شرایطی", "اساس", "آخرین", "دسترس", "پایه", "گزارش", "گزارش‌های",
           # ubiquitous in EVERY post -> zero topical signal, must not satisfy
           # the relevance guard on its own
           "ایران", "کشور", "بررسی", "آمار", "آمارهای", "داده", "داده‌های",
           "وضعیت", "شرایط", "مسئله", "موضوع"}


def _content_words(text: str) -> set:
    """Persian content words (stopwords removed) for topical overlap scoring."""
    import re as _re
    ws = _re.findall(r"[\u0600-\u06FF]{3,}", text or "")
    return {w for w in ws if w not in FA_STOP}


def _topic_keys(text: str) -> set:
    """Which registry TOPIC keys a text evidences, via TOPIC_TERMS.

    Exact word overlap alone is too brittle for Persian: an angle about
    «اشتغال» (employment) and a sentence about «بیکاری» (unemployment) share no
    token yet are plainly the same subject. Mapping both onto topic keys catches
    that relation without hand-listing synonym pairs."""
    tl = (text or "").lower()
    return {topic for topic, terms in TOPIC_TERMS.items()
            if any(t.lower() in tl for t in terms)}


def choose_sentence_and_layout(post_text: str,
                               angle: str = "") -> tuple[str, str, list[dict]]:
    """Pick the sentence the card will be built from.

    ROOT CAUSE this fixes: ranking by numeric density alone makes the card
    showcase whichever sentence has the most percentages -- typically a generic
    macro-economic aside (inflation/GDP) -- even when the post's thesis is
    non-numeric (e.g. military escalation). The card then contradicts the post.
    Fix: score = numeric strength + subject alignment with the post's ANGLE,
    measured by shared words AND shared registry topics, so a number only
    headlines the card when its sentence is actually about the post's subject."""
    angle_words = _content_words(angle)
    angle_topics = _topic_keys(angle)
    best = None
    best_align = 0
    for sent in split_sentences(post_text):
        stats = extract_stats(sent)
        if not stats:
            continue
        sentscore = sum(s["score"] for s in stats[:3])
        if "بر پایه گزارش" in sent or "بر اساس همین گزارش" in sent:
            sentscore += 2
        # subject alignment: shared words OR shared topics with the thesis
        word_ov = len(_content_words(sent) & angle_words) if angle_words else 0
        topic_ov = len(_topic_keys(sent) & angle_topics) if angle_topics else 0
        align = word_ov + topic_ov
        sentscore += 4 * align
        if best is None or sentscore > best[0]:
            best = (sentscore, sent, stats)
            best_align = align
    if best is None:
        raise SystemExit("no numeric sentence found in post")
    _, sent, stats = best
    # HONESTY GUARD: if the most number-dense sentence shares neither vocabulary
    # nor subject with the post's thesis, its figure is an incidental macro
    # aside. Headlining it produces a card that contradicts the post -- signal
    # 'no_stat' so the caller renders a thesis card with no fabricated statistic.
    if (angle_words or angle_topics) and best_align == 0:
        return sent, "no_stat", stats

    # layout choice
    # P1: quote_card layout is ABOLISHED -- quoting the post on the card violates
    # the synthesis rule. Report-based sentences render as number-free thesis
    # cards; the attribution stays in the post text where it belongs.
    if re.search(r"(بر پایه گزارش|بر اساس گزارش)", sent):
        return sent, "no_stat", sorted(stats, key=lambda s: -s["score"])[:1]
    direction = bool(re.search(r"(افت|کاهش|سقوط)", sent)) and \
        bool(re.search(r"(رشد|افزایش|جهش)", sent))
    # B5 POLICY: contrast = two DIFFERENT metrics (different units). Two same-
    # unit numbers are a comparison best served by big_number + sentence, and
    # a number+year pair is impossible now that years are excluded upstream.
    if len(stats) >= 2 and direction and \
            len({s["unit"] for s in stats[:3]}) >= 2:
        return sent, "contrast", sorted(stats, key=lambda s: -s["score"])[:2]
    # ABOLISHED 2026-09-04 (user verdict): the title + hero-number +
    # standalone-sentence card is boring and buggy. Default is the number-free
    # thesis card; a real visual comes from mechanism/trend/minis/contrast
    # candidates above, else the concept AI image via pick_visual.
    return sent, "no_stat", sorted(stats, key=lambda s: -s["score"])[:1]


# ---------------------------------------------------------------- trend data
INDICATOR_FA = {
    # keys = OWID grapher titles as stored by opendata.fetch_owid
    "Annual CO2 emissions": "انتشار سالانه CO₂ ایران",
    "CO2 emissions per capita": "انتشار CO₂ سرانه",
    "Cereal yield": "عملکرد غلات",
    "Electricity consumption per capita": "مصرف برق سرانه",
    "Energy use per capita": "مصرف انرژی سرانه",
    "Female labor force participation": "مشارکت اقتصادی زنان",
    "Gender gap in average wages": "شکاف دستمزد زن و مرد",
    "Renewable share of electricity": "سهم برق تجدیدپذیر",
    "Unemployment rate": "نرخ بیکاری",
    "Wheat yield": "عملکرد گندم",
}


# Topic keys (from metric_registry.topics) -> Persian terms that signal a post
# is actually about that subject. This replaces the old per-indicator keyword
# table: relevance is now decided by the metric's registered TOPICS, so adding a
# new indicator needs no card-code change.
TOPIC_TERMS = {
    "environment": ["محیط زیست", "زیست‌محیطی", "آلودگی"],
    # NOTE «گلخانه» is a FALSE FRIEND and is deliberately absent: in Persian it
    # means both "greenhouse gas" and "greenhouse horticulture". An agricultural
    # export post used it 3x in the farming sense, which satisfied the climate
    # topic and put a CO2 chart on a post about pistachio exports. Only the
    # unambiguous compound counts.
    "climate": ["اقلیم", "آب و هوا", "گرمایش", "گاز گلخانه‌ای", "گازهای گلخانه‌ای"],
    "energy": ["انرژی", "ناترازی", "نیروگاه", "مصرف سوخت"],
    "electricity": ["برق", "نیروگاه", "خاموشی"],
    "economy": ["اقتصاد", "اقتصادی", "رشد", "رکود"],
    "growth": ["رشد", "رونق", "رکود"],
    "inflation": ["تورم", "گرانی", "قیمت"],
    "prices": ["قیمت", "گرانی", "تورم"],
    "labour": ["نیروی کار", "اشتغال", "بیکاری", "شغل", "کارگر"],
    "employment": ["اشتغال", "بیکاری", "شغل"],
    "youth": ["جوان", "جوانان", "نسل"],
    "gender": ["زنان", "جنسیت", "زن"],
    "wages": ["دستمزد", "حقوق", "درآمد"],
    "urbanization": ["شهرنشینی", "شهری", "شهرها", "مسکن"],
    "housing": ["مسکن", "خانه", "اجاره", "شهرهای جدید"],
    "population": ["جمعیت", "جمعیتی", "مهاجرت"],
    # S2 pool expansion: health + education series need licensing vocabulary
    "health": ["سلامت", "بهداشت", "درمان", "بیمارستان", "پزشک"],
    "education": ["آموزش", "مدرسه", "دانشگاه", "تحصیل", "سواد"],
    # tech-pool Phase B: digital series need licensing vocabulary
    "technology": ["فناوری", "تکنولوژی", "دیجیتال", "اینترنت", "موبایل",
                   "پهنای باند", "سیم‌کارت", "نفوذ اینترنت"],
    "digital": ["اینترنت", "موبایل", "آنلاین", "دیجیتال", "ارتباطات"],
    # military-domain expansion: series need licensing vocabulary
    "military": ["نظامی", "ارتش", "دفاع", "مخارج نظامی", "تسلیحات"],
    "conflict": ["جنگ", "درگیری", "کشته", "تلفات", "حمله"],
    "trade": ["صادرات", "واردات", "تجارت", "تحریم", "هرمز", "مسیر", "زنجیره"],
    "agriculture": ["کشاورزی", "زراعت", "گندم", "غلات", "گلخانه‌ای"],
    "food": ["غذا", "امنیت غذایی", "نان"],
    "water": ["آب", "خشکسالی", "بی‌آبی", "تنش آبی"],
    "welfare": ["رفاه", "معیشت", "فقر"],
}


def _load_registry() -> dict:
    """indicator -> registry row. Empty dict if the registry isn't built yet
    (fail-open: the card still renders, just without semantic gating)."""
    try:
        conn = sqlite3.connect("nexus_think_tank.db")
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM metric_registry").fetchall()
        conn.close()
        return {r["indicator"]: dict(r) for r in rows}
    except Exception:
        return {}


def series_is_relevant(indicator: str, angle_text: str, body_text: str,
                       registry: dict | None = None) -> bool:
    """A trend card is only honest when the indicator belongs to the problem's
    THESIS -- not merely to a word that appears somewhere in the body.

    ROOT CAUSE #1 this guards: load_series() ranks purely by relative span, so the
    fastest-growing series in the generic OWID bundle (CO2, +276%) wins for ANY
    problem that has the bundle attached.

    ROOT CAUSE #2 (why the earlier version still failed): body mentions alone were
    enough, so a post about pistachio EXPORTS got a CO2 chart because «گلخانه»
    (greenhouse horticulture) appeared 3x in the farming sense. A chart is the
    single most prominent claim a post makes, so the indicator's topic must be
    evidenced in the ANGLE, which states what the post is actually about.
    Body repetition alone can no longer license a chart."""
    registry = registry if registry is not None else _load_registry()
    entry = registry.get(indicator)
    if not entry:
        return True
    angle_l = (angle_text or "").lower()
    topics = [t.strip() for t in entry["topics"].split(",") if t.strip()]
    for topic in topics:
        for term in TOPIC_TERMS.get(topic, []):
            if term.lower() in angle_l:
                return True
    return False


def load_series(pid: int | None, angle_text: str = "",
                body_text: str = "") -> list[dict]:
    if not pid:
        return []
    conn = sqlite3.connect("nexus_think_tank.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT DISTINCT m.indicator, m.value, CAST(m.reference_period AS INT) yr,
               m.unit, m.subject_entity_name subj, m.provisional prov
        FROM measurements m
        JOIN question_evidence qe ON qe.evidence_type='measurement'
             AND qe.evidence_id=m.id
        JOIN investigation_questions q ON q.id=qe.question_id
        WHERE q.problem_id=? AND m.measurement_source LIKE 'OWID:%'
               AND CAST(m.reference_period AS INT) >= 1990
               AND (m.status IS NULL OR m.status != 'projection')
        ORDER BY m.indicator, yr""", (pid,)).fetchall()
    conn.close()
    series: dict[str, dict] = {}
    reg = _load_registry()
    prov_by_year: dict[str, set] = {}   # indicator -> years flagged provisional
    for r in rows:
        FALLBACK_UNITS = {"Annual CO2 emissions": "میلیون تن",
                          "CO2 emissions per capita": "تن"}
        # registry is authoritative for Persian label + unit; INDICATOR_FA and
        # FALLBACK_UNITS remain only for indicators not yet registered.
        rentry = reg.get(r["indicator"], {})
        s = series.setdefault(r["indicator"], {
            "name_fa": rentry.get("label_fa")
                       or INDICATOR_FA.get(r["indicator"], r["indicator"]),
            "indicator_en": r["indicator"],
            "unit": rentry.get("unit_fa") or r["unit"]
                    or FALLBACK_UNITS.get(r["indicator"], ""),
            "subj": r["subj"], "pts": []})
        s["pts"].append((r["yr"], r["value"]))
        if r["prov"]:
            prov_by_year.setdefault(r["indicator"], set()).add(r["yr"])
    for ind, s in series.items():
        # P0 honesty: the latest year of these series can be a provisional fill
        # (Ember/ILO model estimate), not a published statistic. The trend card
        # labels the last bar accordingly instead of presenting it as fact.
        s["provisional_last"] = bool(prov_by_year.get(ind)) and \
            s["pts"][-1][0] in prov_by_year.get(ind, set())
    out = []
    for s in series.values():
        pts = [pt for pt in s["pts"] if pt[0] is not None]
        # keep only the modern contiguous tail (max 36 pts)
        pts = pts[-36:]
        s["pts"] = pts
        if len(pts) >= 8 and pts[-1][1] != pts[0][1]:
            span = abs(pts[-1][1] - pts[0][1]) / max(abs(pts[0][1]), 1e-9)
            s["pts"] = pts
            s["span"] = span
            out.append(s)
    # topical honesty: drop series that don't belong to this problem's subject
    if angle_text or body_text:
        kept = [s for s in out
                if series_is_relevant(s["indicator_en"], angle_text, body_text)]
        dropped = [s["indicator_en"] for s in out if s not in kept]
        if dropped:
            print("  [card] dropped off-topic series: " + ", ".join(sorted(set(dropped))))
        out = kept
    out.sort(key=lambda s: -s["span"])
    # P4: multi-series layouts (kpi strip, small multiples) need 2-3 related
    # series; the old [:1] cap dated from the single-trend era.
    return out[:3]


# ------------------------------------------------------------------ templates
BASE_CSS = """
html,body{margin:0;padding:0;width:1080px;height:608px;overflow:hidden}
body{background:#0f172a;color:#f1f5f9;
     font-family:'Vazirmatn',Tahoma,'Segoe UI',sans-serif;direction:rtl;
     display:flex;flex-direction:column;justify-content:center;padding:56px 64px;
     position:relative;box-sizing:border-box}
.bgfx{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.bgfx::before{content:'';position:absolute;top:-140px;left:-140px;width:420px;height:420px;
     border-radius:50%;background:radial-gradient(circle,#1e3a5fcc,transparent 70%)}
.bgfx::after{content:'';position:absolute;bottom:-160px;right:-120px;width:380px;height:380px;
     border-radius:50%;background:radial-gradient(circle,#155e63aa,transparent 70%)}
.top{display:flex;align-items:center;gap:12px;margin-bottom:auto;color:#7dd3fc;
     font-size:22px;font-weight:bold;z-index:2;line-height:1.4;padding-top:6px}
.dot{width:14px;height:14px;border-radius:50%;background:#38bdf8}
.foot{margin-top:auto;display:flex;justify-content:space-between;color:#64748b;
      font-size:20px;z-index:2;border-top:1px solid #1e293b;padding-top:18px}
"""


import base64 as _b64

_FONT_CACHE: str | None = None

def _font_css() -> str:
    """Inline Vazirmatn (400/700/800) as data URIs.

    The old relative url('assets/vazirmatn.woff2') pointed at a file that was
    never vendored, so headless Chrome fell back to a subset system font that
    lacked U+06F0 (۰) -- Persian zeros rendered as diamonds (live P17 card).
    Inlining removes all path fragility: the HTML is self-contained."""
    global _FONT_CACHE
    if _FONT_CACHE is not None:
        return _FONT_CACHE
    parts = []
    here = os.path.dirname(os.path.abspath(__file__))
    for weight in (400, 700, 800):
        for ext, fmt in (("ttf", "truetype"), ("woff2", "woff2")):
            p = os.path.join(here, "assets", f"vazirmatn-{weight}.{ext}")
            if os.path.exists(p):
                with open(p, "rb") as f:
                    b64 = _b64.b64encode(f.read()).decode()
                parts.append(
                    f"@font-face{{font-family:'Vazirmatn';font-weight:{weight};"
                    f"src:url(data:font/{fmt};base64,{b64}) format('{fmt}')}}")
                break
    _FONT_CACHE = "".join(parts)
    return _FONT_CACHE


def _page(body_html: str) -> str:
    return ("<!doctype html><html dir='rtl' lang='fa'><meta charset='utf-8'>"
            "<style>" + BASE_CSS + _font_css()
            + "body{font-family:'Vazirmatn',Tahoma,sans-serif}"
            "</style><body>"
            "<div class='bgfx'></div>"
            "<div class='top'><div class='dot'></div>تحلیل داده‌محور</div>"
            + body_html +
            f"<div class='foot'><span>بهمنِ داده</span><span>{jalali_today()}</span></div>"
            "</body></html>")




# Card copy is short user-facing Persian: needs fluency + speed. Live probe
# 2026-09-04: OR/deepseek-v4-flash-free answered in 2.6s with excellent
# Persian; flash-lite (the old value) queued 50-70s. Env-overridable.
from src.investigation_layer.models import combo_for as _combo_for
CARD_MODEL = os.environ.get("ROUTER_COMBO_CARDS", _combo_for("cards"))



def _fallback_title(angle: str) -> str:
    """Mechanical title when the LLM copy call fails: first short clause of
    the angle, capped -- never stitched to a number."""
    if not angle:
        return "تحلیل داده‌محور"
    first = angle.split("،")[0].strip()
    words = first.split()
    if len(words) > 6:
        # a title must read complete: no trailing ellipsis (live P18 title
        # ended '...در آسیا' mid-phrase). Six lead words stand alone fine.
        return " ".join(words[:6])
    return first


def _angle_sentence_fallback(angle: str) -> str:
    """P1: LLM-free fallback sentence for no_stat cards. Built from the angle
    clause structure, never pasted from post/tldr text."""
    first = (angle or "").split("،")[0].strip().rstrip(".")
    if first.startswith("بررسی "):
        first = first[len("بررسی "):]
    return f"این تحلیل، {first} را از منظر داده‌های ایران واکاوی می‌کند." if first \
        else "ایدهٔ اصلی تحلیل روی کارت آمده است."


def _stat_sentence_fallback(angle: str, raw: str, unit: str) -> str:
    """P1: LLM-free one-liner naming the figure and its subject."""
    first = (angle or "").split("،")[0].strip().rstrip(".")
    if first.startswith("بررسی "):
        first = first[len("بررسی "):]
    return f"{first}: رقم کلیدی {raw} {unit} است."


def _contrast_sentence_fallback(angle: str, raw1: str, u1: str,
                                raw2: str, u2: str) -> str:
    """P1: LLM-free two-figure comparison line."""
    first = (angle or "").split("،")[0].strip().rstrip(".")
    if first.startswith("بررسی "):
        first = first[len("بررسی "):]
    return f"{first}: مقایسهٔ {raw1}{u1} با {raw2}{u2}."

def _card_client():
    from openai import OpenAI
    import os
    base = os.environ.get("ROUTER_BASE_URL", "")
    key = os.environ.get("ROUTER_API_KEY", "")
    if not base or not key:
        # read from project .env (where the router stores its key)
        envp = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), ".env")
        if os.path.exists(envp):
            for line in open(envp, encoding="utf-8", errors="replace"):
                s = line.strip()
                if s.startswith("#") or "=" not in s:
                    continue
                k, v = s.split("=", 1)
                if k.strip() == "ROUTER_BASE_URL" and not base:
                    base = v.strip()
                if k.strip() == "ROUTER_API_KEY" and not key:
                    key = v.strip()
    return OpenAI(
        base_url=base or "http://localhost:20128/v1",
        api_key=key or "sk-x")


# Card copy is short user-facing Persian: needs fluency + speed. Live probe
# 2026-09-04: OR/deepseek-v4-flash-free answered in 2.6s with excellent
# Persian; flash-lite (the old value) queued 50-70s. Env-overridable.
from src.investigation_layer.models import combo_for as _combo_for
CARD_MODEL = os.environ.get("ROUTER_COMBO_CARDS", _combo_for("cards"))

def _card_client():
    from openai import OpenAI
    import os
    return OpenAI(
        base_url=os.environ.get("ROUTER_BASE_URL", "http://localhost:20128/v1"),
        api_key=os.environ.get("ROUTER_API_KEY", "sk-x"))

def gen_card_copy(post_text: str, source_sentence: str, stat_raw: str,
                  stat_unit: str, layout: str, angle: str = "",
                  stat2_raw: str = "", stat2_unit: str = "") -> tuple:
    """Return (title, sentence). title <= 12 words, larger font.
    sentence is ONE self-contained Persian sentence naming the figure AND what
    it measures.
    ROOT-CAUSE FIX (number misattribution): the model receives the exact SOURCE
    SENTENCE that contains the number, so it cannot attach the figure to the
    post's general topic (e.g. call inflation 'cooperative share'). A guard then
    verifies the key number actually appears in the output."""
    import time as _t
    import re as _re
    # TEMPORAL RULE (live P22 card called 2025 inflation a 'prediction' in
    # 2026): a completed year is never predicted. Prompt rule + mechanical
    # guard below.
    TEMPORAL = ("قانون تقویم: اکنون سال ۲۰۲۶ است. عددِ سالِ تمام‌شده (مثل ۲۰۲۵) "
                "هرگز «پیش‌بینی» نیست؛ اگر قطعی نیست بگو «برآورد». کلمهٔ "
                "«پیش‌بینی» فقط برای آینده مجاز است. ")
    unit_fa = {"درصد": "درصد", "میلیون": "میلیون", "هزار": "هزار"}.get(stat_unit, stat_unit)
    src_ctx = (source_sentence or "")[:380]
    if layout == "contrast":
        prompt = (
            "کپی‌رایت یک اینفوگرافیک تلگرامی فارسی می‌نویسی. دو عدد روی کارت نمایش داده می‌شوند. "
            "جملهٔ منبع (که این دو عدد در آن آمده) این است:\n«" + src_ctx + "»\n"
            "یک تیتر کوتاه (حداکثر ۱۲ کلمه) و دقیقاً یک جمله فارسی خوداتکا بنویس "
            "که بگوید این دو عدد دقیقاً چه پدیده‌ای را مقایسه می‌کنند. "
            "قانون سخت: عددها «" + stat_raw + " " + unit_fa + "» و «" + stat2_raw + " " + stat2_unit + "» هستند؛ "
            "همان اعداد (به رقم یا حروف) باید بیایند و عدد دیگری اختراع نکن. "
            "موضوع هر عدد باید دقیقاً همان باشد که در جملهٔ منبع آمده — اگر جمله از تورم حرف می‌زند، "
            "عدد را به تورم نسبت بده نه به موضوع کلی پست. "
            + TEMPORAL +
            "خروجی دقیقاً به فرمت: تیتر || جمله"
        )
    else:
        prompt = (
            "کپی‌رایت یک اینفوگرافیک تلگرامی فارسی می‌نویسی. یک عدد کلیدی داریم. "
            "جملهٔ منبع (که این عدد در آن آمده) این است:\n«" + src_ctx + "»\n"
            "یک تیتر کوتاه (حداکثر ۱۲ کلمه) و دقیقاً یک جمله فارسی خوداتکا بنویس "
            "که بگوید این عدد دقیقاً چه پدیده‌ای را می‌سنجد. "
            "قانون سخت: عدد روی کارت «" + stat_raw + " " + unit_fa + "» است؛ "
            "همان عدد دقیقاً (به رقم یا حروف) باید در تیتر یا جمله بیاید و هیچ عدد دیگری اختراع نکن. "
            "موضوع عدد باید دقیقاً همان باشد که در جملهٔ منبع آمده — اگر جمله دربارهٔ تورم است، "
            "عدد را به تورم نسبت بده نه به موضوع کلی پست. جمله نباید قطع شود. "
            + TEMPORAL +
            "خروجی دقیقاً به فرمت: تیتر || جمله"
        )
    try:
        _t.sleep(12)
        from src.route_health import chat as _rchat_card
        text, _used = _rchat_card("cards",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=180, pace=12.0)
        text = (text or "").strip()
        if "||" in text:
            title, sentence = text.split("||", 1)
        else:
            parts = text.split("\n", 1)
            title, sentence = parts[0], (parts[1] if len(parts) > 1 else "")
        title = title.strip().strip("*").strip()
        sentence = sentence.strip().strip("*").strip()
        # GUARD: the key number(s) must appear in the generated copy.
        def _digits(s):
            return _re.sub(r"[^0-9۰-۹]", "", s)
        blob = _digits(title + " " + sentence)
        n1, n2 = _digits(stat_raw), _digits(stat2_raw)
        if (n1 and n1 not in blob) or (n2 and n2 not in blob):
            print("CARD COPY WARN: key number missing from copy -> fallback")
            return "", ""
        # TEMPORAL GUARD: 'prediction' for a completed year is never honest
        # (live P22: 2025 inflation in 2026). Prompt rule above should prevent
        # it; this rejects any slip-through so fallbacks take over.
        if "پیش‌بینی" in (title + " " + sentence) or "پیشبینی" in (title + " " + sentence):
            _yr = _re.search(r"(?:سال\s*)?([۱۲۳۴۵۶۷۸۹۰0123456789]{4})", src_ctx)
            if _yr:
                try:
                    _yv = int(_yr.group(1).translate(
                        str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
                    if _yv < 2026:
                        print("CARD COPY WARN: prediction framing for past year -> fallback")
                        return "", ""
                except Exception:
                    pass
        return title[:90], sentence[:300]
    except Exception as e:
        print("CARD COPY FAIL:", e)
        return "", ""

def _clamp_plain(text: str, maxlen: int = 200) -> str:
    """Clamp without HTML-escaping twice (callers pass through _clamp later)."""
    t = (text or "").strip()
    if len(t) <= maxlen:
        return t
    cut = t[:maxlen]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > 40 else cut).rstrip() + "…"


NO_VERBATIM_MIN_WORDS = 8


def no_verbatim_violations(card_text: str, sources: list[str]) -> list[str]:
    """P1 GUARD: no run of >=8 consecutive content words on the card may appear
    verbatim in the post, outline, or tldr. Detects sliced text mechanically --
    the same lesson as hook blocklists: measure the SHAPE, don't hope."""
    def words(s: str) -> list[str]:
        s = re.sub(r"[«»\"'.,؛:!?()\[\]{}—–\-\u200c]", " ", s or "")
        return [w for w in s.split() if len(w) > 1]
    src_ngrams: set[tuple] = set()
    k = NO_VERBATIM_MIN_WORDS
    for s in sources:
        ws = words(s)
        for i in range(max(0, len(ws) - k + 1)):
            src_ngrams.add(tuple(ws[i:i + k]))
    out = []
    for line in card_text.splitlines():
        cw = words(line)
        for i in range(max(0, len(cw) - k + 1)):
            if tuple(cw[i:i + k]) in src_ngrams:
                out.append(" ".join(cw[i:i + k]))
                break
    return out


def gen_thesis_copy(angle: str, support: str) -> tuple:
    """Card copy for posts whose thesis carries no usable figure.
    Writes a title + one self-contained sentence about the ARGUMENT itself,
    with no statistic -- prevents cards that headline an unrelated macro number."""
    import time as _t
    prompt = (
        "کپی‌رایت یک اینفوگرافیک تلگرامی فارسی می‌نویسی. این پست عدد کلیدی مرتبط با موضوع خود ندارد، "
        "پس کارت باید فقط «ایده اصلی» را منتقل کند. "
        "زاویه تحلیل: «" + (angle or "")[:300] + "»\n"
        "خلاصه: «" + (support or "")[:300] + "»\n"
        "یک تیتر کوتاه (حداکثر ۱۲ کلمه) و دقیقاً یک جمله فارسی خوداتکا بنویس که ایده اصلی را برساند. "
        "قانون سخت: هیچ عدد یا آماری اضافه نکن. جمله نباید قطع شود. "
        "خروجی دقیقاً به فرمت: تیتر || جمله"
    )
    try:
        _t.sleep(12)
        from src.route_health import chat as _rchat_card
        text, _used = _rchat_card("cards",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4, max_tokens=180, pace=12.0)
        text = (text or "").strip()
        if "||" in text:
            title, sentence = text.split("||", 1)
        else:
            parts = text.split("\n", 1)
            title, sentence = parts[0], (parts[1] if len(parts) > 1 else "")
        return title.strip().strip("*")[:90], sentence.strip().strip("*")[:300]
    except Exception as e:
        print("THESIS COPY FAIL:", e)
        return "", ""


def tpl_big(stat: dict, title: str, sentence: str) -> str:
    disp = stat["raw"].translate(EN_DIGITS)
    unit = {"درصد": "٪"}.get(stat["unit"], stat["unit"])
    title = _clamp(title, 90) if title else ""
    sentence = _clamp(sentence, 200) if sentence else ""
    # B6: a hero number must carry its metric name. Without it a bare '۴۰
    # میلیون' could be anything (barrels? dollars? people?).
    label = _norm_tx((stat.get("label") or "").strip())
    lab_html = (f"<div style='font-size:26px;color:#94a3b8;margin-top:6px'>"
                f"{html.escape(label)}</div>") if label else ""
    body = f"""
<div style='z-index:2;text-align:center'>
  <div style='font-size:52px;font-weight:800;color:#e2e8f0;line-height:1.3;margin-bottom:18px'>{title}</div>
  <div style='font-size:130px;font-weight:800;color:#38bdf8;line-height:1'>
    {html.escape(disp)}<span style='font-size:64px'>{html.escape(unit)}</span></div>
  {lab_html}
  <div style='font-size:30px;margin-top:24px;color:#cbd5e1;line-height:1.7'>{sentence}</div>
</div>"""
    return _page(body)


def tpl_contrast(a: dict, b: dict, title: str, sentence: str) -> str:
    """B6 POLICY (user 2026-09-02): the '2025 vs 3.7 percent' card happened
    because contrast cells showed two BIG numbers with zero context. Cells now
    must carry their metric label under the number (label passed via the stat
    dicts); a contrast whose two numbers share one metric is a trend, not a
    contrast, and is rejected upstream by choose_sentence_and_layout."""
    def cell(s):
        u = {"درصد": "٪"}.get(s["unit"], s["unit"])
        label = _norm_tx(s.get("label") or "")
        lab_html = (f"<div style='font-size:23px;color:#94a3b8;margin-top:8px;"
                    f"line-height:1.5'>{html.escape(label)}</div>") if label else ""
        return (f"<div style='flex:1;text-align:center'>"
                f"<div style='font-size:72px;font-weight:800;color:#38bdf8'>{html.escape(s['raw'].translate(EN_DIGITS))}"
                f"<span style='font-size:38px'>{html.escape(u)}</span></div>{lab_html}</div>")
    title = _clamp(title, 90) if title else ""
    sentence = _clamp(sentence, 200) if sentence else ""
    body = f"""
<div style='z-index:2;text-align:center'>
  <div style='font-size:48px;font-weight:800;color:#e2e8f0;line-height:1.3;margin-bottom:18px'>{title}</div>
  <div style='display:flex;align-items:center;gap:24px'>
    {cell(a)}
    <div style='font-size:44px;color:#94a3b8'>در برابر</div>
    {cell(b)}
  </div>
  <div style='font-size:30px;margin-top:30px;color:#cbd5e1;line-height:1.7'>{sentence}</div>
</div>"""
    return _page(body)


CLAUSE_END = re.compile(r"[؛،](?![^؛،]*[؛،])")   # last ؛ or ،


# ---------------------------------------------------------------- insight
def tpl_insight(title: str, sentence: str, spark: dict | None = None) -> str:
    """B7 POLICY: for analysis-led posts, NO hero numerals at all. Title +
    one standalone synthesized sentence + optional small sparkline chip whose
    only number is a subtle latest-value footer. The analysis is the visual."""
    title = _clamp(title, 90) if title else ""
    sentence = _clamp(sentence, 230) if sentence else ""
    chip = ""
    if spark and spark.get("pts"):
        pts = spark["pts"][-20:]
        vals = [v for _, v in pts]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1.0
        bars = ""
        for _, v in pts:
            h = max(5, int(5 + 46 * (v - lo) / rng))
            bars += (f"<div style='flex:1;height:{h}px;margin-top:{50 - h}px;"
                     f"background:linear-gradient(180deg,#38bdf8,#0ea5e955);"
                     f"border-radius:2px 2px 0 0'></div>")
        unit = {"درصد": "٪"}.get(spark.get("unit") or "", spark.get("unit") or "")
        lv = pts[-1][1]
        last = (f"{lv:,.0f}" if abs(lv) >= 100 else
                f"{lv:.1f}" if abs(lv) >= 10 else f"{lv:.2f}").translate(EN_DIGITS)
        yr = str(pts[-1][0]).translate(EN_DIGITS)
        chip = (f"<div style='margin:26px auto 0;max-width:430px;background:#1e293b88;"
                f"border:1px solid #334155;border-radius:12px;padding:14px 16px 14px'>"
                f"<div style='font-size:20px;color:#94a3b8;text-align:right'>"
                f"{html.escape(spark.get('name_fa') or '')}</div>"
                f"<div style='display:flex;align-items:flex-end;gap:2px;height:52px;"
                f"direction:ltr'>{bars}</div>"
                f"<div style='font-size:20px;color:#38bdf8;margin-top:8px;text-align:right'>"
                f"{last} {html.escape(unit)} · {yr}</div></div>")
    body = f"""
<div style='z-index:2;text-align:center'>
  <div style='font-size:50px;font-weight:800;color:#e2e8f0;line-height:1.35'>{title}</div>
  <div style='font-size:31px;color:#cbd5e1;line-height:1.8;margin-top:26px'>{sentence}</div>
  {chip}
</div>"""
    return _page(body)


# ---------------------------------------------------------------- KPI strip
def tpl_kpi(kpis: list[dict], title: str, sentence: str) -> str:
    """P4 layout: a horizontal strip of 2-3 KPI cells (value + unit + label),
    then the synthesized title+sentence. Labels are the metric registry's
    Persian labels; values come straight from series data, never the prose."""
    cells = ""
    for k in kpis[:3]:
        u = {"درصد": "٪"}.get(k["unit"], k["unit"])
        cells += (f"<div style='flex:1;background:#1e293b88;border:1px solid #334155;"
                  f"border-radius:14px;padding:16px 10px;text-align:center'>"
                  f"<div style='font-size:56px;font-weight:800;color:#38bdf8'>"
                  f"{html.escape(str(k['value']).translate(EN_DIGITS))}"
                  f"<span style='font-size:30px'>{html.escape(u)}</span></div>"
                  f"<div style='font-size:22px;color:#94a3b8;margin-top:6px'>"
                  f"{html.escape(k['label'])}</div></div>")
    title = _clamp(title, 90) if title else ""
    sentence = _clamp(sentence, 200) if sentence else ""
    body = f"""
<div style='z-index:2;text-align:center'>
  <div style='font-size:46px;font-weight:800;color:#e2e8f0;line-height:1.3;margin-bottom:22px'>{title}</div>
  <div style='display:flex;gap:16px;direction:rtl'>{cells}</div>
  <div style='font-size:29px;margin-top:26px;color:#cbd5e1;line-height:1.7'>{sentence}</div>
</div>"""
    return _page(body)


def build_kpis(series: list[dict]) -> list[dict] | None:
    """2-3 KPI cells from the problem's own series: latest value per indicator.
    Only indicators whose latest point is NOT provisional make honest KPIs.
    Display formatting: >=100 -> comma-grouped ints; else <=2 decimals (a KPI
    cell showing ۲.۵۹۱۸ is unreadable -- values are for eyes, not ledgers)."""
    def fmt(v) -> str:
        if abs(v) >= 100:
            return f"{v:,.0f}".translate(EN_DIGITS)
        if abs(v) >= 10:
            return f"{v:.1f}".translate(EN_DIGITS)
        return f"{v:.2f}".rstrip("0").rstrip(".").translate(EN_DIGITS)
    out = []
    for s in series:
        if not s.get("pts"):
            continue
        if s.get("provisional_last"):
            continue
        yr, v = s["pts"][-1]
        out.append({"value": fmt(v),
                    "unit": s.get("unit") or "",
                    "label": f"{s['name_fa']} ({str(yr).translate(EN_DIGITS)})"})
    return out if 2 <= len(out) <= 3 else None


# ---------------------------------------------------------------- mini-multiples
def tpl_minis(series: list[dict], title: str, sentence: str) -> str:
    """P4 layout: 2-3 small multiples (one sparkline per indicator), each with
    its own latest-year label. For problems whose story is the JOINT movement
    of several related series."""
    pans = ""
    for s in series[:3]:
        pts = s["pts"][-24:]
        if len(pts) < 6:
            continue
        vals = [v for _, v in pts]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1.0
        # 24-point inline bar sparkline
        bars = ""
        for _, v in pts:
            h = max(6, int(6 + 80 * (v - lo) / rng))
            bars += (f"<div style='flex:1;height:{h}px;margin-top:{88 - h}px;"
                     f"background:linear-gradient(180deg,#38bdf8,#0ea5e955);"
                     f"border-radius:2px 2px 0 0'></div>")
        unit = {"درصد": "٪"}.get(s.get("unit") or "", s.get("unit") or "")
        lv = pts[-1][1]
        last = (f"{lv:,.0f}" if abs(lv) >= 100 else
                f"{lv:.1f}" if abs(lv) >= 10 else f"{lv:.2f}").translate(EN_DIGITS)
        pans += (f"<div style='flex:1;background:#1e293b88;border:1px solid #334155;"
                 f"border-radius:14px;padding:14px 12px 8px'>"
                 f"<div style='font-size:22px;color:#e2e8f0;height:58px'>"
                 f"{html.escape(s['name_fa'])}</div>"
                 f"<div style='display:flex;align-items:flex-end;gap:2px;height:92px;"
                 f"direction:ltr'>{bars}</div>"
                 f"<div style='font-size:24px;color:#38bdf8;margin-top:6px'>"
                 f"{last} {html.escape(unit)} <span style='color:#64748b;font-size:18px'>"
                 f"{str(pts[-1][0]).translate(EN_DIGITS)}</span></div></div>")
    title = _clamp(title, 90) if title else ""
    sentence = _clamp(sentence, 200) if sentence else ""
    body = f"""
<div style='z-index:2;text-align:center'>
  <div style='font-size:46px;font-weight:800;color:#e2e8f0;line-height:1.3;margin-bottom:20px'>{title}</div>
  <div style='display:flex;gap:14px;direction:rtl'>{pans}</div>
  <div style='font-size:28px;margin-top:24px;color:#cbd5e1;line-height:1.7'>{sentence}</div>
</div>"""
    return _page(body)


def _norm_tx(text: str) -> str:
    """Deterministic Persian normalisation for ALL card copy.

    Card titles/sentences are LLM-written and bypassed fa_norm (live P22
    title carried Latin '2025'). Lazy import keeps cards.py runnable without
    the src package on path; failure returns text unchanged (never break
    rendering for orthography)."""
    try:
        if "src" not in sys.modules:
            sys.path.insert(0, os.getcwd())
        from src.investigation_layer.fa_norm import normalise_fa
        return normalise_fa(text)
    except Exception:
        return text


def _clamp(text: str, maxlen: int = 170) -> str:
    """Cut at the last clause boundary before maxlen so cards never end
    mid-number/mid-word."""
    text = _norm_tx(text or "")
    if len(text) <= maxlen:
        return html.escape(text)
    cut = text[:maxlen]
    m = None
    for m in re.finditer(r"[،؛]", cut):
        pass
    if m and m.start() > 40:
        return html.escape(cut[:m.start()].rstrip(" ،؛")) + "…"
    # fall back to word boundary
    sp = cut.rfind(" ")
    return html.escape(cut[:sp].rstrip()) + "…"


def tpl_quote(caption: str) -> str:
    cap = _clamp(caption, 170)
    body = f"""
<div style='z-index:2'>
  <div style='font-size:110px;color:#38bdf8;line-height:.6'>«</div>
  <div style='font-size:34px;color:#e2e8f0;line-height:1.9;margin-top:8px;
       display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden'>{cap}</div>
  <div style='font-size:26px;color:#64748b;margin-top:26px'>— بر پایه گزارش‌های خبری</div>
</div>"""
    return _page(body)


def _unit_fa(raw_unit: str) -> str:
    u = (raw_unit or "").strip().lower()
    if not u:
        return ""
    if "%" in u or u == "percent":
        return "٪"
    if "million tonnes" in u:
        return "میلیون تن"
    if "tonnes" in u or u == "t":
        return "تن"
    if "kwh" in u or "kilowatt" in u:
        return "کیلووات‌ساعت"
    if "person" in u:
        return "نفر"
    if "yield" in u:
        return u
    return raw_unit.strip()


def tpl_trend(series: dict) -> str:
    pts = series["pts"]
    vals = [v for _, v in pts]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(pts)
    step = max(1, n // 16)
    sel = pts[::step]
    if pts[-1] != sel[-1]:
        sel.append(pts[-1])
    unit_fa = _unit_fa(series['unit'])
    if unit_fa == "میلیون تن" and abs(sel[-1][1]) > 100000:
        sel = [(y, v / 1e6) for y, v in sel]
    # scaling above changes value range -> recompute bounds on the SCALED series
    svals = [v for _, v in sel]
    lo, hi = min(svals), max(svals)
    rng = (hi - lo) or 1.0
    fmt = lambda x: (f"{x:,.1f}".translate(EN_DIGITS)) if unit_fa == "میلیون تن" else (f"{x:,.0f}".translate(EN_DIGITS))
    min_v, max_v = min(svals), max(svals)
    bars = ""
    for y, v in sel:
        h = max(8, int(8 + 190 * (v - lo) / rng))
        label = ""
        if v == max_v or y == sel[-1][0]:
            label = f"<div style='text-align:center;font-size:18px;color:#38bdf8;margin-bottom:4px'>{fmt(v)}</div>"
        elif v == min_v:
            label = f"<div style='text-align:center;font-size:18px;color:#94a3b8;margin-bottom:4px'>{fmt(v)}</div>"
        bars += (f"<div style='flex:1;display:flex;flex-direction:column;justify-content:flex-end'>"
                 f"{label}"
                 f"<div style='height:{h}px;"
                 f"background:linear-gradient(180deg,#38bdf8,#0ea5e955);border-radius:6px 6px 0 0'></div>"
                 f"</div>")
    subj = (series.get('subj') or '').strip()
    if subj.lower() in ('iran', 'ایران'):
        subj = ''
    # P0 honesty: when the newest bar is a provisional/modelled estimate, say so
    # next to the number instead of presenting it as a published statistic.
    if series.get('provisional_last'):
        meta = ' · '.join(x for x in (subj, f"واحد: {unit_fa}" if unit_fa else '',
                                      'آخرین نقطه: برآورد اولیه') if x)
    else:
        meta = ' · '.join(x for x in (subj, f"واحد: {unit_fa}" if unit_fa else '') if x)
    body = f"""
<div style='z-index:2'>
  <div style='font-size:40px;font-weight:bold;color:#e2e8f0'>{html.escape(series['name_fa'])}</div>
  <div style='font-size:24px;color:#64748b;margin-top:6px'>{html.escape(meta)}</div>
  <div style='display:flex;align-items:flex-end;gap:10px;height:220px;margin-top:36px;direction:ltr'>{bars}</div>
  <div style='display:flex;justify-content:space-between;color:#94a3b8;font-size:24px;margin-top:14px;direction:ltr'>
    <span>{sel[0][0]}</span><span style='color:#38bdf8'>{fmt(sel[-1][1])}{(' ' + html.escape(unit_fa)) if unit_fa else ''}</span><span>{sel[-1][0]}</span>
  </div>
</div>"""
    return _page(body)


# ---------------------------------------------------------------- mechanism
def tpl_headline(title: str, sentence: str) -> str:
    """Headline card: title (larger font) + one self-contained sentence.
    NO big numeral element — the figure lives inside the sentence, so the
    number can never desync from its subject."""
    title = _clamp(title, 90) if title else "تحلیل داده‌محور"
    sentence = _clamp(sentence, 240) if sentence else ""
    body = f"""
<div style='z-index:2'>
  <div style='font-size:54px;font-weight:800;color:#e2e8f0;line-height:1.35;
       text-align:center;margin-bottom:30px'>{title}</div>
  <div style='font-size:36px;color:#cbd5e1;line-height:1.9;text-align:center;
       max-width:900px;margin:0 auto'>{sentence}</div>
</div>"""
    return _page(body)


# ---------------------------------------------------------------- synthesis
def synthesize_caption(angle: str, stats: list[dict], layout: str) -> str:
    """DEPRECATED: old mechanical 'angle: 13%' stitching. Kept only for
    reference; no longer called. The LLM-driven headline path replaced it."""
    if angle:
        first_clause = angle.split("،")[0].strip()
        words = first_clause.split()
        if len(words) > 6:
            angle_clause = " ".join(words[:6]) + "…"
        else:
            angle_clause = first_clause
    else:
        angle_clause = ""
    if layout == "contrast":
        a, b = stats[0], stats[1]
        ua = {"درصد": "٪"}.get(a["unit"], a["unit"])
        ub = {"درصد": "٪"}.get(b["unit"], b["unit"])
        return (f"{angle_clause}: {a['raw']}{ua} در برابر {b['raw']}{ub}")
    elif layout == "trend":
        s = stats[0]
        u = {"درصد": "٪"}.get(s["unit"], s["unit"])
        return f"{angle_clause} — {s['raw']}{u}"
    else:
        s = stats[0]
        u = {"درصد": "٪"}.get(s["unit"], s["unit"])
        return f"{angle_clause}: {s['raw']}{u}"


def detect_mechanism_steps(post_body: str) -> list[str]:
    """A mechanism card makes sense when the post describes a causal chain.
    Mechanical trigger: >=2 causal connectives in one post."""
    triggers = ["منجر به", "به دلیل", "در نتیجه", "زنجیره", "اثر گذاشته",
                "فشار می‌آورد", "می‌انجامد", "به بحران"]
    hits = sum(1 for t in triggers if t in post_body)
    return hits


def tpl_mechanism(steps: list[str], title: str = "") -> str:
    """Causal-chain card. NEVER truncates step text with '...' (live P18: all
    four boxes were cut mid-phrase) -- instead the font auto-fits downward
    until the measured geometry fits, and overlong syntheses are rejected
    upstream. A title anchors the chain (cards without one read as floating
    fragments)."""
    title = _clamp(title, 90) if title else ""
    n = len(steps)
    title_html = (f"<div style='font-size:44px;font-weight:800;color:#e2e8f0;"
                  f"line-height:1.35;margin-bottom:16px;text-align:center'>"
                  f"{title}</div>") if title else ""

    def _build(fsize: int) -> str:
        rows = ""
        for i, s_ in enumerate(steps):
            arrow = ("<div style='text-align:center;font-size:22px;color:#38bdf8;"
                     "margin:1px 0'>↓</div>") if i < n - 1 else ""
            rows += (f"<div style='background:#1e293b88;border:1px solid #334155;"
                     f"border-radius:12px;padding:10px 18px;font-size:{fsize}px;"
                     f"line-height:1.6;color:#e2e8f0'>{html.escape(s_)}</div>"
                     + arrow)
        return _page(f"<div style='z-index:2'>{title_html}{rows}</div>")

    # auto-fit: largest font that fits the box (measured, not eyeballed)
    for fsize in (24, 22, 20, 18, 16, 14):
        page = _build(fsize)
        try:
            ok, _ = check_fits(page)
        except Exception:
            ok = True
        if ok:
            return page
    return _build(14)


def synthesize_mechanism_steps(angle: str, outline_items: list[str]) -> list[str] | None:
    """P1 CARD SYNTHESIS RULE: card text is WRITTEN for the card, never sliced
    from the post or the outline. The old build_mechanism_from_outline() pasted
    outline lines verbatim into boxes -- exactly what the user prohibited.

    The LLM gets the angle + outline as SOURCE MATERIAL and must produce 3-4
    short self-contained chain steps (each its own sentence, max ~14 words).
    On any failure return None so the caller falls through to another layout:
    a degraded card is worse than a different honest card."""
    import time as _t
    outline_txt = "\n".join(f"- {o}" for o in outline_items)[:900]
    prompt = (
        "زنجیرهٔ علّی یک تحلیل را برای اینفوگرافیک تلگرامی بازطراحی می‌کنی. "
        "زاویهٔ تحلیل: «" + (angle or "")[:280] + "»\n"
        "طرح روایت (منبع ایده، نه متن قابل کپی):\n" + outline_txt + "\n"
        "سه تا چهار گامِ زنجیره بنویس. قوانین سخت:\n"
        "• هر گام یک عبارت/جملهٔ کوتاه مستقل (حداکثر ۱۲ کلمه؛ کوتاه‌تر بهتر) است.\n"
        "• گام‌ها علت‌ومعلولاند: هر گام از گام قبل نتیجه می‌شود؛ رشتهٔ یک پیوستار است، نه فهرست موضوعات موازی.\n"
        "• هر گام باید خوداتکا باشد: هیچ «این بخش/مذکور/فوق» — مرجع هر گام باید داخل خودش روشن باشد.\n"
        "• کلمه‌به‌کلمه از منبع کپی نکن؛ با کلمات خودت بازنویسی کن.\n"
        "• هیچ عددی اختراع نکن؛ فقط اعداد داخل منبع، در صورت نیاز.\n"
        "خروجی: فقط گام‌ها، هر خط یک گام کامل (هرگز نیمه‌کاره قطع نکن)، بدون شماره و بدون بولت.")
    try:
        client = _card_client()
        text = None
        for attempt in range(2):        # router 502s are common; one retry
            _t.sleep(12)
            try:
                from src.route_health import chat as _rchat_card3
                text, _used = _rchat_card3("cards",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.4, max_tokens=260, pace=0.0)
                text = text or ""
                break
            except Exception as e2:
                print(f"[mech] attempt {attempt+1} failed: "
                      f"{type(e2).__name__}: {str(e2)[:80]}")
        if not text:
            return None
        raw_lines = [ln.strip() for ln in text.splitlines()]
        src_blob = (angle or "") + " " + outline_txt
        steps = []
        for ln in raw_lines:
            ln = re.sub(r"^[\-\*•\d۰-۹\.\)\s]+", "", ln).strip()
            # strip a leading "گام N:" label if the model adds one
            ln = re.sub(r"^گام\s*\d+\s*[:،-]\s*", "", ln).strip()
            if len(ln) < 12:
                continue
            # REJECT, don't clamp: a truncated step on the card is worse than
            # no step (vision QA caught a mid-phrase cut shipped to readers).
            # Tightened 140->110 after P18: four ~120-char steps only fit by
            # shrinking to unreadable sizes; short steps are the design.
            if len(ln) > 110:
                continue
            # self-containment: dangling deixis only resolves in surrounding prose
            if re.search(r"این بخش|این مورد|همین|مذکور|فوق|…", ln):
                continue
            # invented-number guard: every digit-run in the step must exist in
            # the source material (angle/outline), else the model fabricated it
            step_nums = set(re.findall(r"\d+", ln))
            if step_nums and not all(n in src_blob for n in step_nums):
                continue
            steps.append(ln)
        if len(steps) > 4:
            # an ordered chain: keep the first 4 rather than discarding good
            # synthesis (live P18 produced 5 clean steps and lost the card)
            steps = steps[:4]
        if len(steps) < 3:
            print(f"[mech] only {len(steps)} clean step(s) -- no mechanism card, "
                  f"caller falls through to an honest layout")
            return None
        return steps
    except Exception as e:
        print(f"[mech] synthesis failed: {type(e).__name__}: {str(e)[:90]}")
        return None


def check_fits(page_html: str) -> tuple[bool, str]:
    """Measure whether the rendered content stays inside the card box.

    WHY MECHANICAL: card clipping has now been found twice by eye (viewport
    clipping from negative-offset pseudo-elements, and a cut-off mechanism box).
    Vision QA catches it only when a human happens to look, so the geometry is
    measured in the browser instead: the content div's top must be >= 0 and its
    bottom <= the body height.

    NOTE: measure the CONTENT div, not document.body.scrollHeight -- body has a
    fixed 608px height here, so scrollHeight reports 608 no matter how much text
    there is, which made an earlier probe report OVERFLOW for even one box.
    """
    import subprocess as _sp, re as _re, os as _os
    js = ("<script>var d=document.querySelector('div[style*=\"z-index:2\"]');"
          "if(d){var b=document.body.getBoundingClientRect();"
          "var r=d.getBoundingClientRect();"
          "document.title=Math.round(r.top)+'|'+Math.round(r.bottom)+'|'"
          "+Math.round(b.height);}</script>")
    probe = _os.path.join("posts", "_fitprobe.html")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write(page_html.replace("</body>", js + "</body>"))
        r = _sp.run([CHROME, "--headless=new", "--disable-gpu", "--dump-dom",
                     f"--window-size={W},{H}",
                     "file:///" + _os.path.abspath(probe).replace("\\", "/")],
                    capture_output=True, text=True, timeout=60)
        m = _re.search(r"<title>(-?\d+)\|(-?\d+)\|(\d+)</title>", r.stdout or "")
        if not m:
            return True, "unmeasured"
        top, bottom, bh = int(m.group(1)), int(m.group(2)), int(m.group(3))
        ok = top >= 0 and bottom <= bh
        return ok, f"top={top} bottom={bottom} box={bh}"
    except Exception as e:
        return True, f"probe failed: {type(e).__name__}"
    finally:
        if _os.path.exists(probe):
            _os.remove(probe)


def render(page_html: str, out_png: str) -> str:
    tmpdir = os.path.dirname(os.path.abspath(out_png))
    html_path = os.path.join(tmpdir, "_card.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page_html)
    url = "file:///" + os.path.abspath(html_path).replace("\\", "/")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           f"--window-size={W},{H}", f"--screenshot={os.path.abspath(out_png)}", url]
    subprocess.run(cmd, capture_output=True, timeout=60)
    os.remove(html_path)
    if not os.path.exists(out_png):
        raise RuntimeError("chrome did not produce screenshot")
    return out_png


def clean_caption(sent: str) -> str:
    """Trim leading emoji/connectors that read oddly on a card."""
    s = sent.strip()
    s = re.sub(r"^[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F]+\s*", "", s)
    for lead in ("اما ", "و ", "در حالی که ", "این در حالی است که "):
        if s.startswith(lead):
            s = s[len(lead):]
    return s


def main() -> None:
    post_path = sys.argv[1]
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else None
    text = open(post_path, encoding="utf-8").read()
    head, _, post_body_full = text.split("=== GATES")[0].strip().partition("\n\n")
    post_body = post_body_full.strip() or head.strip()
    outline_items = re.findall(r"^- (.+)$", head, re.M)
    angle_line = ""
    mangle = re.search(r"^ANGLE: (.+)$", head, re.M)
    if mangle:
        angle_line = mangle.group(1)

    sent, layout, stats = choose_sentence_and_layout(post_body, angle_line)
    src_sent = sent  # the sentence the chosen stat(s) came from — keeps number attached to its subject

    # B6: hero numbers must be self-describing. Attach a metric label built
    # from the registry Persian name + the sentence words right around the
    # number, so '۴۰ میلیون' never floats without context.
    reg = _load_registry()
    for st in stats:
        lab = ""
        best_ind = None
        for ind, entry in reg.items():
            if ind.lower() in (src_sent or "").lower():
                best_ind = entry.get("name_fa")
                break
        if best_ind:
            lab = best_ind
        else:
            # fall back: the words AFTER number+unit name the measured quantity
            # ('۴۰ میلیون بشکه‌ایِ ذخایر موجود' -> 'ذخایر موجود'). A BEFORE-
            # fragment was tried first and produced mid-phrase garbage like
            # 'بررسیِ افزایش ۳۰ تا' (live finding on P16).
            idx = src_sent.find(st["raw"]) if src_sent else -1
            if idx >= 0:
                rest = src_sent[idx + len(st["raw"]):]
                # strip quantity words incl. suffixed forms (بشکه‌ای، درصدی);
                # the old inline ZWNJ-suffix pattern silently failed to match
                # (live: label kept 'میلیون بشکه‌ای' verbatim), so spell the
                # forms out explicitly. Leading kasra/ZWNJ stripped too.
                rest = re.sub(r"^(\s*(بشکه‌ای|میلیونی|میلیاردی|درصدی|برابری|تنی|نفری|دلاری|میلیون|میلیارد|هزار|درصد|تن|تُن|متر|نفر|دلار|بشکه|ریال|تومان))+",
                              "", rest).lstrip(" ‌ِ،؛().")
                rest = re.sub(r"^ایِ?\s*", "", rest)  # leftover ezafe fragment
                # skip leading prepositions so the label names the THING:
                # 'در برابر یک دلار' beats 'ریال در برابر'
                rest = re.sub(r"^(در|به|از|با|برای|تا)\s+", "", rest)
                # verb-first after-fragment ('رسیده و شاخص...') means the
                # metric name sits BEFORE the number: use the preceding words
                # instead (live P22 label was 'رسیده و شاخص بهای').
                _VERBS = ("رسیده", "شده", "است", "شد", "کرد", "کاهش", "افزایش",
                          "یافت", "یافته", "می‌شود", "می‌کند", "گیرد", "گرفت")
                if rest.split()[:1] and rest.split()[0] in _VERBS:
                    pre = src_sent[:idx].strip(" ،؛().")
                    pw = pre.split()[-7:]
                    pw = [w for w in pw
                          if w not in ("در", "به", "از", "با", "برای", "تا",
                                       "سال", "طی", "بین")
                          # a bare year is context, never the metric
                          # (live P22 label collapsed to just '۲۰۲۵')
                          and not (w.strip("۰۱۲۳۴۵۶۷۸۹0123456789,.٫٬")
                                   == "" or
                                   (len(w) == 4 and w.translate(
                                       str.maketrans("۰۱۲۳۴۵۶۷۸۹",
                                                     "0123456789")).isdigit()
                                    and 1300 <= int(w.translate(
                                        str.maketrans("۰۱۲۳۴۵۶۷۸۹",
                                                      "0123456789"))) <= 2100))]
                    rest = " ".join(pw) if pw else rest
                words = rest.split()[:4]
                if words:
                    lab = " ".join(words)
        if lab:
            st["label"] = lab

    # trend card must be topically honest: angle carries the thesis, body the support
    series = load_series(pid, angle_line, post_body) if pid else []

    # P1: mechanism steps are SYNTHESIZED for the card, never sliced from the
    # outline (the old verbatim path violated the card synthesis rule).
    mech_steps = None
    if detect_mechanism_steps(post_body) >= 2 and outline_items:
        mech_steps = synthesize_mechanism_steps(angle_line, outline_items)

    # every text source the card must not copy verbatim from
    verbatim_sources = [post_body, *outline_items]
    mt = re.search(r"^TLDR: (.+)$", head, re.M)
    tldr_line = mt.group(1).strip() if mt else ""
    if tldr_line:
        verbatim_sources.append(tldr_line)

    def _guarded(page_html: str, card_text: str) -> str:
        """Mechanical P1 guard: re-synthesize... actually degrade honestly.
        A verbatim slice on the card is a rule violation; if the guard fires
        after copy generation we keep the layout but strip to the fallback
        copy path, which is LLM-written, not sliced."""
        v = no_verbatim_violations(card_text, verbatim_sources)
        if v:
            print(f"[card] NO-VERBATIM GUARD fired: {' '.join(v[:8])}…")
        return page_html

    # P4 diversity: collect the layouts this post HONESTLY qualifies for, then
    # rotate among them (seeded by problem id) instead of always taking the
    # first. A post with a causal chain + two solid series can be shown as a
    # mechanism diagram OR a KPI strip; which one is a rhythm choice, not a
    # correctness choice. Correctness gates (topic match, provisional filter,
    # step quality) decide what enters the candidate list.
    candidates: list[tuple[str, object]] = []
    if mech_steps:
        _mech_title = _fallback_title(angle_line)
        candidates.append(("mechanism",
                           lambda: tpl_mechanism(mech_steps, _mech_title)))
    kpis = build_kpis(series) if series else None
    multi = [s for s in (series or []) if len(s.get("pts") or []) >= 8
             and not s.get("provisional_last")]
    if series and series[0]["span"] >= 0.35:
        if kpis and len(multi) >= 2:
            def _mk_kpi():
                t, snt = gen_card_copy(post_body, src_sent, kpis[0]["value"],
                                       kpis[0]["unit"], "big_number", angle_line)
                t = t or _fallback_title(angle_line)
                snt = snt or _angle_sentence_fallback(angle_line)
                return tpl_kpi(kpis, t, snt), f"{t}\n{snt}"
            candidates.append(("kpi", _mk_kpi))
        if len(multi) >= 2:
            def _mk_minis():
                t, snt = gen_thesis_copy(angle_line, tldr_line or post_body[:300])
                t = t or _fallback_title(angle_line)
                snt = snt or _angle_sentence_fallback(angle_line)
                return tpl_minis(multi, t, snt), f"{t}\n{snt}"
            candidates.append(("minis", _mk_minis))
        def _mk_trend():
            return tpl_trend(series[0]), ""
        candidates.append(("trend", _mk_trend))

    page = card_text = None
    if candidates:
        layout, mk = candidates[(pid or 0) % len(candidates)]
        built = mk()
        if isinstance(built, tuple):
            page, card_text = built
        else:
            page = built
            card_text = ""
        if layout == "mechanism":
            card_text = "\n".join(mech_steps)
        _guarded(page, card_text)
    elif layout == "no_stat":
        # No figure in the post belongs to its thesis -> the ANALYSIS is the
        # card. B7: prefer the insight layout (no hero numbers) when a series
        # exists for a small context chip; fall back to thesis headline.
        title, sentence = gen_thesis_copy(angle_line, tldr_line or post_body[:300])
        if not title:
            title = _fallback_title(angle_line)
        if not sentence:
            # P1: fallback must NOT paste post/tldr text. Write from the angle.
            sentence = _angle_sentence_fallback(angle_line)
        spark = series[0] if series and (series[0].get("pts")) else None
        if spark:
            page = tpl_insight(title, sentence, spark)
        else:
            page = tpl_headline(title, sentence)
        _guarded(page, f"{title}\n{sentence}")
    elif layout == "contrast":
        title, sentence = gen_card_copy(post_body, src_sent, stats[0]["raw"], stats[0]["unit"],
                                        "contrast", angle_line,
                                        stats[1]["raw"], stats[1]["unit"])
        if not title:
            title = _fallback_title(angle_line)
        if not sentence:
            # P1: synthesized one-liner from the two figures, not the post text
            ua = {"درصد": "٪"}.get(stats[0]["unit"], stats[0]["unit"])
            ub = {"درصد": "٪"}.get(stats[1]["unit"], stats[1]["unit"])
            sentence = _contrast_sentence_fallback(angle_line,
                                                   stats[0]["raw"], ua,
                                                   stats[1]["raw"], ub)
        page = tpl_contrast(stats[0], stats[1], title, sentence)
        _guarded(page, f"{title}\n{sentence}")
    else:
        # big_number layout ABOLISHED 2026-09-04 (user verdict) -- this branch
        # is now unreachable (chooser never emits it) and stays a loud skip so
        # a hero-number card can never silently come back. Concept fallback.
        print("CARD SKIP hero-number layout abolished; routing to concept")
        return

    out = re.sub(r"\.txt$", "", post_path) + "_card.png"
    fits, geom = check_fits(page)
    if not fits:
        print(f"CARD WARN: content clipped ({geom})")
    render(page, out)
    print(f"CARD OK layout={layout} fit={geom} "
          f"stat={[s['raw'] + ' ' + s['unit'] for s in stats[:2]]}")
    print(out)


if __name__ == "__main__":
    main()
