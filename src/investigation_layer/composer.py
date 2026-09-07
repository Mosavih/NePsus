"""T3 (V0.1) -- Persian post composer with presentation-format flexibility.

Turns an evidence dossier into an engaging, human-readable Persian Telegram post.

V0.1 changes over V0:
  - pick_format(): deterministically choose a presentation shape from the
    dossier (narrative / qa / listicle / data_spotlight / myth_fact) so posts
    are not all the same skeleton.
  - Longer & more informative: target 1400-1800 chars, require >=2 concrete
    data points and >=1 actionable lever/insight.
  - Adds a format-specific instruction block to the composer prompt.
  - Composer sees ONLY the dossier (traceability by construction); the
    fabrication guard (T5) enforces this mechanically.
"""
from __future__ import annotations

from src.investigation_layer.dossier import build_dossier, format_dossier_for_composer
from src.investigation_layer.extraction import _client
from src.investigation_layer.models import model_for as _model_for


# Composer model: tiered (2026-09-04 audit). The old setup hardcoded
# flash-lite as the workhorse, but live census showed lite queueing 50-70s
# per call while 3.6-flash answers in ~5s -- and the hardest job (writing)
# deserves better than the smallest model. Env-overridable.
def _default_composer_model() -> str:
    return _model_for("compose")


DEFAULT_COMPOSER_MODEL = _model_for("compose")

COMPOSER_SYS = (
    "You are the chief editor of a Persian-language science-policy Telegram "
    "channel. Your readers are educated general audience, NOT academics. You "
    "write in fluent, natural PERSIAN (Farsi). You never fabricate: every "
    "number and factual claim you write must come from the evidence given to "
    "you. If evidence is about other countries, you say so plainly."
)

# ---- Presentation formats (V0.1 flexibility) -----------------------------
# Each block is injected into the composer prompt when that format is chosen.
FORMAT_BLOCKS = {
    "narrative": (
        "قالب: روایت (narrative). با یک قلاب انسانی شروع کن (مثلاً سناریویی از "
        "زندگی روزمره)، شواهد را در دل داستان بیاور، و با یک بینش صادقانه تمام کن."),
    "qa": (
        "قالب: پرسش‌وپاسخ (Q&A). با یک سؤال کنجکاوی‌برانگیز از مخاطب شروع کن "
        "(مثلاً «چرا ...؟»)، سپس در ۲ تا ۴ پرسش و پاسخ کوتاه، شواهد را بیاور. "
        "هر پاسخ یک پاراگراف کوتاه باشد."),
    "listicle": (
        "قالب: فهرست بینش (listicle). با یک جمله قلاب شروع کن، سپس ۳ تا ۵ نکته "
        "شماره‌گذاری‌شده بنویس که هر کدام یک یافته یا عدد مشخص از شواهد را "
        "منعکس کند. در آخر یک جمع‌بنده یک‌خطی."),
    "data_spotlight": (
        "قالب: کانون داده (data spotlight). روی یک یا دو عددِ شگفت‌انگیز/مهم "
        "تمرکز کن؛ عدد را برجسته کن، سپس توضیح بده چرا مهم است و چه معنایی "
        "برای ایران دارد. یک تحلیل کوتاه بعد از عددها."),
    "myth_fact": (
        "قالب: باور غلط در برابر واقعیت (myth vs fact). یک باور رایج/ساده‌انگارانه "
        "درباره موضوع را مطرح کن (با «شاید فکر کنید...»)، سپس با شواهد ردّش کن "
        "یا اصلاحش کن. در آخر یک جمله واقعیت‌محور."),
}


def pick_format(dossier: dict) -> str:
    """Deterministic format choice from the dossier shape (explainable)."""
    cov = dossier.get("coverage", {})
    n_find = cov.get("findings", 0)
    n_meas = cov.get("measurements", 0)
    has_iran = cov.get("iran_specific_findings", 0) > 0
    has_comp = cov.get("comparator_only", False)
    # Data spotlight when we have hard measurement series.
    if n_meas >= 2:
        return "data_spotlight"
    # Listicle when several distinct findings to enumerate.
    if n_find >= 3:
        return "listicle"
    # Myth/fact when evidence is all comparator (good for 'you might think X').
    if has_comp and not has_iran:
        return "myth_fact"
    # Q&A for a focused single finding with a lever.
    if n_find == 1:
        return "qa"
    # Default: narrative.
    return "narrative"


COMPOSER_USR_TMPL = """شواهد زیر درباره یک مسئله در ایران است:

--- شروع شواهد ---
{dossier}
--- پایان شواهد ---

یک پست تلگرام به فارسی بنویس.

قالب انتخابی برای این مسئله: {fmt_block}

الزامات:
1. برای مخاطب عام؛ نه زبان آکادمیک. جمله‌های کوتاه. لحن علمی اما گرم و جذاب.
2. قلاب قوی در شروع (چرا این موضوع برای زندگی روزمره مردم مهم است).
3. هر عدد و ادعا باید عیناً از شواهد بالا باشد. هیچ عددی از خودت نساز.
   اعداد بزرگ را خوانا بنویس: همیشه گرد با حداکثر یک رقم اعشار و مقیاس فارسی
   («حدود ۱۱۱٫۹ میلیارد دلار») — نوشتن ارقام کامل (مثل ۱۱۱٬۹۲۸٬۸۶۳٬۱۸۸)
   اکیداً ممنوع. درصدها را دقیق نگه دار.
3.5. **انتساب یک‌باره (ممنوعیت تکرار منبع):** منبع هر سنجه را فقط یک بار،
   کنار اولین عددش بیاور؛ در ادامهٔ متن نه نام منبع را تکرار کن نه سال را
   توضیح بده — سالِ روی عدد خودش افشاست. فهرست کامل منابع در بخش «منابع» ی
   پست می‌آید و تکرار آن در متن ممنوع. قالب دقیق (کپی کن): «قیمت نفت برنت
   (یاهو فایننس)، سپتامبر ۲۰۲۶: ۹۵٫۶۳ دلار» — نام ارائه‌دهنده داخل پرانتز
   بعد از نام سنجه، بعد سال، بعد عدد. نوشتن جای‌نگهدار ([منبع]، [...]،
   X/Y، «منبع: .») اکیداً ممنوع؛ اگر منبع را نمی‌دانی از همین قالبِ بخش
   شواهد استفاده کن، هرگز جای خالی نگذار.
4. اعداد فقط در خدمت تز: هر عدد باید ستون فقرات روایت را جلو ببرد. اگر سنجه‌ای
به تز ربط ندارد، حذفش کن — هیچ «حداقل تعدادی» در کار نیست؛ پستی با یک عددِ
دقیقِ مرتبط، بهتر از پستی با سه عددِ بی‌ربط است.
5. اگر شواهد مربوط به کشورهای دیگر است (نه ایران)، صریح بگو که این «تجربه کشورهای
   دیگر» است و چرا برای ایران مرتبط است.
6. در انتها یک «بینش یا اقدام ممکن» کوتاه بده (چه می‌شد کرد / چه عاملی کلیدی است) —
   فقط اگر شواهد واقعاً از آن حمایت می‌کند؛ در غیر این صورت حذف کن.
7. یک جمله کوتاه و طبیعی درباره محدودیت شواهد (اگر هست) — نه لزوماً با تیتر.
7.5. **روایتِ امروز — سه حالت (حیاتی):**
   • حالت ۱ (ANALYSIS CONTEXT موجود): پست باید حول تحلیل‌های پس از بحران ساخته شود؛
     داده‌های آماری را به‌عنوان «نقطه شروع/پیش از بحران» در دل همان روایت بیاور.
   • حالت ۲ (فقط NEWS CONTEXT بدون تحلیل بیرونی): روایت را بر «چهره اقتصاد پیش از
     بحران» بنا کن و صریح بگو داده رسمی پس از بحران هنوز در دسترس نیست.
   • حالت ۳ (بخش پیش‌بینی خودمان): اگر بلوک OWN FORECAST در شواهد هست، یک بخش کوتاه
     «چه انتظاری داریم؟» بنویس که فقط از همین استدلال‌ها استفاده کند و با عبارت
     «بر اساس همین داده‌ها» آغاز شود. هیچ عددی خارج از شواهد نیاور.
   در هر سه حالت هیچ عدد آماری از خبرها نیاور؛ خبر فقط برای اهمیت امروز مسئله است.
8. **صداقت زمانی (حیاتی):** تاریخ امروز در ابتدای شواهد آمده. هرگز داده قدیمی را
   «الان» یا «امروز» معرفی نکن. سال کنار عدد (قانون ۳٫۵) به‌تنهایی افشای قدمت
   است — پاراگراف جداگانه دربارهٔ اینکه «داده‌ها قدیمی‌اند» ممنوع. تنها
   استثنا: اگر تزِ پست به «اکنون» تکیه می‌کند و برای سنجهٔ اصلی هیچ عدد
   ۲۰۲۵+ نیست، همین نبود را یک‌جمله‌ای و صریح بگو. اعداد پیش‌بینی
   (پروژکشن) را فقط با برچسب «پیش‌بینی/برآورد» و سال دقیق بیاور؛ هرگز پیش‌بینیِ
   سال گذشته را به‌عنوان آینده معرفی نکن. روند را از کل بازه سری بساز، نه از دو
   نقطه دست‌چین‌شده — FACTS زمانی که برای هر سری نوشته شده معتبر است و نباید نقض شود.
9. **انسجام روایت (یک ستون فقرات):** پست باید یک خط روایی واحد داشته باشد که همه
   اعداد در خدمت آن باشند؛ از تیترهای بخش‌بندی جدا و مستقل (مثل «کانون داده»،
   «تحلیل کوتاه»، «تجربه دیگران») پرهیز کن مگر قالب listicle. پاراگراف‌ها با
   عبارت‌های ربط به هم دوخته شوند تا خواننده یک روند فکری پیوسته را دنبال کند.
10. ایموجی را در جاهای مناسب و مرتبط به کار ببر (قلاب، تفکیک بخش‌ها، جمع‌بندی) — تا ۵ تا مجاز است اما فقط
   وقتی واقعاً به خوانایی یا حس پیام کمک می‌کند؛ هرگز تحمیلی یا بی‌ربط.
11. طول: بین ۱۴۰۰ تا ۱۸۰۰ کاراکتر (مفصل‌تر و آموزنده‌تر از قبل).
12. فقط متن پست را بده؛ بدون هیچ توضیح اضافه، بدون پیش‌نویس، بدون ترجمه.
13. **فارسی طبیعی (حیاتی — خطاهای واقعی پست‌های قبلی):** هر جمله حداکثر ~۲۰
کلمه؛ بلندتر شد حتماً با نقطه بشکن. «به عنوان» حداکثر یک بار در هر جمله
(«به عنوان X به عنوان Y» ممنوع). قید احتیاط («برآورد اولیه») فقط یک بار در
هر پاراگراف، نه کنار تک‌تک اعداد. لحن یکدست رسمی-صمیمی: اصطلاح عامیانه
(مثل «دست‌وپا زدن») در متن تحلیلی ممنوع. پرانتز توضیحی حداکثر یک سطر؛
پرانتزِ چندسریِ پر از عدد و ارجاع ممنوع — آن را به جمله‌ای مستقل تبدیل کن.
زنجیرهٔ «چرا که...، ... و با...، ... را می‌کند» (سه بند پیاپی در یک جمله)
ممنوع؛ هر بند، جملهٔ خودش.

پست:"""


def build_dossier_and_text(db, problem_id: int):
    """Return (dossier_text, dossier_dict) for the composer/reviewer."""
    d = build_dossier(db, problem_id)
    return format_dossier_for_composer(d), d


def compose_post(db, problem_id: int, model: str | None = None,
                 temperature: float = 0.7) -> dict:
    d = build_dossier(db, problem_id)
    if not d["coverage"]["has_evidence"]:
        return {"ok": False, "reason": "no linked evidence to compose from",
                "problem_id": problem_id}
    dossier_text = format_dossier_for_composer(d)
    fmt = pick_format(d)
    fmt_block = FORMAT_BLOCKS.get(fmt, FORMAT_BLOCKS["narrative"])
    from src.route_health import chat as _rchat
    post, _used = _rchat("compose",
        messages=[{"role": "system", "content": COMPOSER_SYS},
                  {"role": "user", "content": COMPOSER_USR_TMPL.format(
                      dossier=dossier_text, fmt_block=fmt_block)}],
        temperature=temperature, model=model)
    post = (post or "").strip()
    if post.startswith("```"):
        post = post.strip("`").lstrip("text\n").strip()
    return {
        "ok": True,
        "problem_id": problem_id,
        "post_fa": post,
        "chars": len(post),
        "format": fmt,
        "model": model or DEFAULT_COMPOSER_MODEL,
        "dossier": d,
    }
