"""One-off: build data/history.json from historical automation records.

Sources are the per-run notes under .workbuddy/memory/ (see the header of each
entry). These weeks ran before the durable ledger existed, so only the digest
values (mention counts / sentiment split) survived — no itemised content. Every
row is therefore tagged ``data_source="backfill"`` so the dashboard can label it
honestly instead of passing it off as freshly crawled data.

Safe to re-run: live weeks already present in the ledger are never overwritten.
"""
import json
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_PATH = os.path.join(BASE_DIR, "data", "history.json")

ALL_BRANDS = ["Fanatec", "Simagic", "Thrustmaster", "PXN", "WINCTRL", "VIRPIL"]

NOTE = ("本周数据由自动化执行记录回填（周次边界按运行日期推断），"
        "仅保留提及量与情感分布，原始条目详情未留存。")

# week_start, week_end, moza_mentions, (pos, neu, neg), competitor counts
BACKFILL = [
    ("2026-08-10", "2026-08-16", 1, (None, None, None),
     {"Fanatec": 5, "Simagic": 25}),
    ("2026-08-31", "2026-09-06", 41, (61, 29, 10),
     {"Fanatec": 9, "Thrustmaster": 4, "Simagic": 1, "PXN": 1}),
    ("2026-09-07", "2026-09-13", 2, (None, None, None),
     {"Fanatec": 1, "Thrustmaster": 14}),
    ("2026-09-14", "2026-09-20", 3, (0, 100, 0),
     {}),
    ("2026-09-21", "2026-09-27", 49, (67, 27, 6),
     {"Fanatec": 17, "Thrustmaster": 3, "Simagic": 30}),
    ("2026-10-05", "2026-10-11", 3, (33, 67, 0),
     {"Fanatec": 3, "Thrustmaster": 4}),
    ("2026-10-12", "2026-10-18", 4, (50, 50, 0),
     {"Fanatec": 5, "Thrustmaster": 2}),
]


def build():
    ledger = {"reports": {}, "competitors": {}}
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            existing = json.load(f)
        if isinstance(existing, dict):
            ledger["reports"] = existing.get("reports", {}) or {}
            ledger["competitors"] = existing.get("competitors", {}) or {}

    added_r, added_c = 0, 0
    for ws, we, mentions, (pos, neu, neg), counts in BACKFILL:
        # --- MOZA report ---
        if ws not in ledger["reports"]:
            if pos is None:
                pos, neu, neg = 0, 100, 0  # sentiment not recorded → all neutral
                sent_note = "（回填：本周情感分布未记录，按中性归集）"
            else:
                sent_note = ""
            ledger["reports"][ws] = {
                "week_start": ws,
                "week_end": we,
                "overall_summary": f"本周 MOZA 相关提及 {mentions} 条。{NOTE}{sent_note}",
                "mention_count": mentions,
                "mention_change": 0,
                "positive_pct": pos,
                "neutral_pct": neu,
                "negative_pct": neg,
                "data_source": "backfill",
                "topics": [{
                    "rank": 1,
                    "topic": "回填周次（无明细）",
                    "summary": NOTE,
                }],
                "risks": [],
                "sentiments": [],
                "actions": [],
            }
            added_r += 1

        # --- competitor counts ---
        if ws not in ledger["competitors"]:
            full = {b: int(counts.get(b, 0)) for b in ALL_BRANDS}
            total = sum(full.values())
            ledger["competitors"][ws] = {
                "week_start": ws,
                "week_end": we,
                "summary": f"本周竞品动态合计 {total} 条（回填：仅计数，无明细）。",
                "data_source": "backfill",
                "counts": full,
                "data": {},
            }
            added_c += 1

    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, ensure_ascii=False, indent=2)

    print(f"ledger: {LEDGER_PATH}")
    print(f"  +{added_r} report-weeks, +{added_c} competitor-weeks")
    print(f"  report weeks   : {sorted(ledger['reports'])}")
    print(f"  competitor weeks: {sorted(ledger['competitors'])}")


if __name__ == "__main__":
    build()
