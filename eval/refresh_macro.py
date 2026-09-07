"""Phase A-1: refresh macro data past the 2024 wall.

- WDI: re-fetch every WB code in metric_registry notes; insert missing
  (indicator, period) rows; flip provisional 2025 -> 0 where WDI now serves
  the same value as a published observation (2026-07 refresh).
- IMF DataMapper: NGDP_RPCH + PCPIPCH for Iran; <=2025 stored as 'estimate',
  2026+ as 'projection' (status-isolated by existing P0 rules, never a card
  hero, never dossier-latest).

Usage: python eval/refresh_macro.py
"""
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from src.investigation_layer.opendata import fetch_worldbank, fetch_imf  # noqa: E402

DB = "nexus_think_tank.db"
NOW = datetime.now(timezone.utc).isoformat()

IMF_NEW = [
    ("NGDP_RPCH", "Real GDP growth, IMF (%)", "رشد واقعی تولید ناخالص داخلی (صندوق بین‌المللی پول)",
     "%", "economy,growth",
     "رشد سالانهٔ اقتصاد به روایت صندوق بین‌المللی پول؛ برآورد و پیش‌بینی"),
    ("PCPIPCH", "Inflation avg, IMF (%)", "تورم میانگین سالانه (صندوق بین‌المللی پول)",
     "%", "economy,inflation,prices",
     "میانگین تورم سالانه به روایت صندوق؛ با آمار رسمی داخلی متفاوت است"),
]


def ensure_imf_registry(conn: sqlite3.Connection) -> None:
    for code, label, fa, unit, topics, gloss in IMF_NEW:
        if conn.execute("SELECT 1 FROM metric_registry WHERE indicator=?",
                        (label,)).fetchone():
            continue
        slug = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
        conn.execute(
            """INSERT INTO metric_registry
               (indicator, canonical_id, label_fa, metric_kind, unit_fa, topics,
                gloss_fa, note) VALUES (?,?,?,?,?,?,?,?)""",
            (label, f"iran.economy.imf_{slug}", fa, "level", unit, topics,
             gloss, f"IMF {code}; Phase-A fast macro 2026-09-03"))
        print(f"[reg] {label}", flush=True)
    conn.commit()


def refresh_wdi(conn: sqlite3.Connection) -> tuple[int, int]:
    pairs = []
    for (note,) in conn.execute(
            "SELECT DISTINCT note FROM metric_registry WHERE note LIKE 'WB %'"):
        m = re.search(r"WB ([A-Z0-9._]+)", note or "")
        if m:
            pairs.append(m.group(1))
    new, unprov = 0, 0
    for code in sorted(set(pairs)):
        labels = [r[0] for r in conn.execute(
            """SELECT indicator FROM metric_registry WHERE note LIKE ?
               ORDER BY indicator""", (f"%{code}%",))]
        try:
            rows = fetch_worldbank(code)
        except Exception as e:
            print(f"[wdi] {code} FETCH FAIL: {type(e).__name__}", flush=True)
            continue
        for r in rows:
            for label in labels:
                have = conn.execute(
                    """SELECT value, provisional FROM measurements
                       WHERE indicator=? AND reference_period=?""",
                    (label, r["reference_period"])).fetchone()
                if have is None:
                    conn.execute(
                        """INSERT INTO measurements
                           (indicator, subject_entity_name, value, unit,
                            reference_period, measurement_source,
                            acquisition_method, collection_time, quality,
                            status, provisional)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (label, "Iran", r["value"], r.get("unit") or "",
                         r["reference_period"], "World Bank WDI",
                         "worldbank_api", NOW, "raw", "raw", 0))
                    new += 1
                elif have[1] == 1 and r["reference_period"] == "2025":
                    # Lifecycle: a finalized value that DIFFERS revises the
                    # row (old code only unflagged identical values, so real
                    # revisions stuck as stale provisionals forever).
                    if abs((have[0] or 0) - r["value"]) < 1e-9:
                        conn.execute(
                            """UPDATE measurements SET provisional=0
                               WHERE indicator=? AND reference_period='2025'""",
                            (label,))
                    else:
                        from eval.score_ledger import ensure as _le, \
                            log_supersede as _ls
                        _le()
                        _ls(conn, label, "2025", have[0], r["value"])
                        conn.execute(
                            """UPDATE measurements
                               SET value=?, provisional=0
                               WHERE indicator=? AND reference_period='2025'""",
                            (r["value"], label))
                    unprov += 1
        conn.commit()
        print(f"[wdi] {code}: {len(rows)} pts", flush=True)
        time.sleep(1.5)
    return new, unprov


def refresh_imf(conn: sqlite3.Connection) -> int:
    ensure_imf_registry(conn)
    n = 0
    for code, label, _fa, _unit, _topics, _gloss in IMF_NEW:
        try:
            rows = fetch_imf(code, "IRN")
        except Exception as e:
            print(f"[imf] {code} FETCH FAIL: {type(e).__name__}", flush=True)
            continue
        for r in rows:
            per = r["reference_period"]
            try:
                yr = int(str(per)[:4])
            except ValueError:
                continue
            if yr < 1990 or yr > 2031:
                continue
            status = "projection" if yr >= 2026 else (
                "estimate" if yr == 2025 else "raw")
            if conn.execute(
                    """SELECT 1 FROM measurements WHERE indicator=?
                       AND reference_period=?""",
                    (label, per)).fetchone():
                continue
            conn.execute(
                """INSERT INTO measurements
                   (indicator, subject_entity_name, value, unit,
                    reference_period, measurement_source, acquisition_method,
                    collection_time, quality, status, provisional)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (label, "Iran", r["value"], r.get("unit") or "%", per,
                 "IMF DataMapper", "imf_api", NOW, "raw", status, 0))
            n += 1
        conn.commit()
        print(f"[imf] {code}: stored (total new so far: {n})", flush=True)
        time.sleep(1.5)
    return n


def main() -> None:
    conn = sqlite3.connect(DB)
    w_new, w_unprov = refresh_wdi(conn)
    i_new = refresh_imf(conn)
    conn.close()
    print(f"DONE: wdi_new={w_new} wdi_unprovisionaled={w_unprov} imf_new={i_new}",
          flush=True)


if __name__ == "__main__":
    main()
