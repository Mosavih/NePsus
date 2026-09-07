"""V1 of the next-generation vision: the semantic metric registry.

WHY THIS EXISTS
The card generator used to ask an LLM "what does 42.2% mean?" by handing it a
Persian sentence, and it answered "the cooperative sector's share" when the
number was actually the inflation rate. The meaning of a number lived only in
prose, so every consumer had to re-derive it -- and could get it wrong.

This module gives every indicator ONE authoritative semantic record:

    canonical_id   stable machine key            iran.cpi.inflation.annual
    label_fa       Persian display label         نرخ تورم مصرف‌کننده
    metric_kind    what KIND of quantity         rate | share | level | index
    unit_fa        Persian unit                  درصد
    topics         which subjects it belongs to  economy, inflation
    polarity       is 'up' good or bad?          bad | good | neutral

With this, downstream code ASKS instead of INFERS:
  - a card reads label_fa/unit_fa -> misattribution becomes impossible
  - topical relevance is a data property (topics), not a keyword heuristic
  - "up is bad" enables honest phrasing without an LLM judgement call

Usage:
    python eval/metric_registry.py init      # create table + seed
    python eval/metric_registry.py report    # coverage vs measurements
"""
import sqlite3
import sys

DB = "nexus_think_tank.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    indicator       TEXT NOT NULL UNIQUE,   -- matches measurements.indicator
    canonical_id    TEXT NOT NULL,
    label_fa        TEXT NOT NULL,
    metric_kind     TEXT NOT NULL,          -- rate|share|level|index|count
    unit_fa         TEXT,
    topics          TEXT NOT NULL,          -- comma-separated topic keys
    polarity        TEXT NOT NULL DEFAULT 'neutral',  -- good|bad|neutral
    higher_is       TEXT,                   -- short Persian gloss of direction
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_metric_registry_indicator
    ON metric_registry(indicator);
"""

# indicator -> (canonical_id, label_fa, kind, unit_fa, topics, polarity, higher_is)
SEED = [
    ("Annual CO2 emissions", "iran.co2.emissions.annual",
     "انتشار سالانه دی‌اکسید کربن", "level", "میلیون تن",
     "environment,climate", "bad", "انتشار بیشتر"),
    ("CO2 emissions per capita", "iran.co2.emissions.percapita",
     "انتشار سرانه دی‌اکسید کربن", "level", "تن",
     "environment,climate", "bad", "انتشار بیشتر"),
    ("Real GDP growth (%)", "iran.gdp.growth.real",
     "رشد واقعی تولید ناخالص داخلی", "rate", "درصد",
     "economy,growth", "good", "رشد بیشتر"),
    ("GDP per capita growth (annual %)", "iran.gdp.percapita.growth",
     "رشد سرانه تولید ناخالص داخلی", "rate", "درصد",
     "economy,growth,welfare", "good", "رشد بیشتر"),
    ("Inflation, consumer prices (annual %)", "iran.cpi.inflation.annual",
     "نرخ تورم مصرف‌کننده", "rate", "درصد",
     "economy,inflation,prices", "bad", "تورم بالاتر"),
    ("Unemployment rate, total (% of labor force)", "iran.unemployment.total",
     "نرخ بیکاری کل", "rate", "درصد",
     "labour,employment,economy", "bad", "بیکاری بیشتر"),
    ("Unemployment, youth ages 15-24 (% of labor force)",
     "iran.unemployment.youth",
     "نرخ بیکاری جوانان (۱۵ تا ۲۴ سال)", "rate", "درصد",
     "labour,employment,youth", "bad", "بیکاری بیشتر"),
    ("Unemployment rate", "iran.unemployment.owid",
     "نرخ بیکاری", "rate", "درصد",
     "labour,employment", "bad", "بیکاری بیشتر"),
    ("Urban population (% of total)", "iran.population.urban.share",
     "سهم جمعیت شهری", "share", "درصد",
     "urbanization,housing,population", "neutral", "شهرنشینی بیشتر"),
    ("Exports of goods and services (% of GDP)", "iran.exports.share.gdp",
     "سهم صادرات کالا و خدمات از تولید ناخالص داخلی", "share", "درصد",
     "trade,economy", "good", "صادرات بیشتر"),
    ("Exports of goods and services (current US$)", "iran.exports.usd",
     "ارزش دلاری صادرات کالا و خدمات", "level", "دلار",
     "trade,economy", "good", "صادرات بیشتر"),
    ("Energy use per capita", "iran.energy.use.percapita",
     "مصرف سرانه انرژی", "level", "کیلووات‌ساعت",
     "energy,environment", "neutral", "مصرف بیشتر"),
    ("Electricity consumption per capita", "iran.electricity.percapita",
     "مصرف سرانه برق", "level", "کیلووات‌ساعت",
     "energy,electricity", "neutral", "مصرف بیشتر"),
    ("Renewable share of electricity", "iran.electricity.renewable.share",
     "سهم برق تجدیدپذیر", "share", "درصد",
     "energy,electricity,environment", "good", "سهم بیشتر تجدیدپذیر"),
    ("Cereal yield", "iran.agri.cereal.yield",
     "عملکرد غلات", "level", "تن در هکتار",
     "agriculture,food,water", "good", "عملکرد بیشتر"),
    ("Wheat yield", "iran.agri.wheat.yield",
     "عملکرد گندم", "level", "تن در هکتار",
     "agriculture,food,water", "good", "عملکرد بیشتر"),
    ("Female labor force participation", "iran.labour.female.participation",
     "نرخ مشارکت نیروی کار زنان", "rate", "درصد",
     "labour,gender,employment", "good", "مشارکت بیشتر"),
]


def init(conn: sqlite3.Connection) -> int:
    conn.executescript(SCHEMA)
    n = 0
    for (ind, cid, label, kind, unit, topics, pol, higher) in SEED:
        cur = conn.execute(
            """INSERT INTO metric_registry
                 (indicator, canonical_id, label_fa, metric_kind, unit_fa,
                  topics, polarity, higher_is)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(indicator) DO UPDATE SET
                 canonical_id=excluded.canonical_id,
                 label_fa=excluded.label_fa,
                 metric_kind=excluded.metric_kind,
                 unit_fa=excluded.unit_fa,
                 topics=excluded.topics,
                 polarity=excluded.polarity,
                 higher_is=excluded.higher_is""",
            (ind, cid, label, kind, unit, topics, pol, higher))
        n += 1
    conn.commit()
    return n


def lookup(conn: sqlite3.Connection, indicator: str) -> dict | None:
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT * FROM metric_registry WHERE indicator=?",
                     (indicator,)).fetchone()
    return dict(r) if r else None


def topics_for(conn: sqlite3.Connection, indicator: str) -> set:
    e = lookup(conn, indicator)
    return set(e["topics"].split(",")) if e else set()


def report(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT m.indicator,
                  COUNT(DISTINCT m.reference_period) yrs,
                  r.label_fa, r.unit_fa, r.polarity
           FROM measurements m
           LEFT JOIN metric_registry r ON r.indicator = m.indicator
           GROUP BY m.indicator ORDER BY yrs DESC""").fetchall()
    covered = sum(1 for r in rows if r["label_fa"])
    print(f"indicators in measurements: {len(rows)} | in registry: {covered}")
    print()
    for r in rows:
        mark = "OK " if r["label_fa"] else "MISS"
        label = r["label_fa"] or "(unmapped)"
        unit = r["unit_fa"] or "-"
        print(f"  [{mark}] yrs={r['yrs']:3d}  {label[:38]:40s} {unit:14s} "
              f"{r['polarity'] or ''}")
    missing = [r["indicator"] for r in rows if not r["label_fa"]]
    if missing:
        print("\nUNMAPPED indicators (add to SEED):")
        for x in missing:
            print("  -", x)


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    conn = sqlite3.connect(DB)
    if cmd == "init":
        n = init(conn)
        print(f"metric_registry ready, {n} indicators seeded")
        report(conn)
    else:
        report(conn)
    conn.close()


if __name__ == "__main__":
    main()
