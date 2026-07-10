"""
MOZA Brand Sentiment Monitor - Web Dashboard
Author: WorkBuddy
"""

import os
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "sentiment.db"

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS weekly_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                week_end   TEXT NOT NULL,
                overall_summary TEXT,
                mention_count  INTEGER DEFAULT 0,
                mention_change REAL DEFAULT 0,
                positive_pct  REAL DEFAULT 0,
                neutral_pct   REAL DEFAULT 0,
                negative_pct  REAL DEFAULT 0,
                raw_data      TEXT,
                created_at    TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(week_start, week_end)
            );

            CREATE TABLE IF NOT EXISTS hot_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                rank INTEGER,
                topic TEXT,
                summary TEXT,
                FOREIGN KEY(report_id) REFERENCES weekly_reports(id)
            );

            CREATE TABLE IF NOT EXISTS risk_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                level TEXT,
                content TEXT,
                source TEXT,
                FOREIGN KEY(report_id) REFERENCES weekly_reports(id)
            );

            CREATE TABLE IF NOT EXISTS sentiment_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                sentiment TEXT,
                viewpoint TEXT,
                source TEXT,
                FOREIGN KEY(report_id) REFERENCES weekly_reports(id)
            );

            CREATE TABLE IF NOT EXISTS action_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_id INTEGER,
                action_text TEXT,
                FOREIGN KEY(report_id) REFERENCES weekly_reports(id)
            );

            CREATE TABLE IF NOT EXISTS competitors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT
            );

            CREATE TABLE IF NOT EXISTS competitor_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                competitor_id INTEGER,
                week_start TEXT NOT NULL,
                week_end   TEXT NOT NULL,
                category   TEXT NOT NULL,
                content    TEXT,
                source     TEXT,
                sentiment  TEXT DEFAULT 'neutral',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(competitor_id) REFERENCES competitors(id)
            );

            CREATE TABLE IF NOT EXISTS competitor_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                week_start TEXT NOT NULL,
                week_end   TEXT NOT NULL,
                summary    TEXT,
                created_at TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(week_start, week_end)
            );
        """)


def seed_competitors(conn):
    """Initialize competitor list if empty."""
    existing = conn.execute("SELECT COUNT(*) FROM competitors").fetchone()[0]
    if existing > 0:
        return
    brands = [
        ("Fanatec", "#f5a623"),
        ("Simagic", "#00c2ff"),
        ("Thrustmaster", "#e53935"),
        ("PXN", "#7c4dff"),
        ("WINCTRL", "#00e676"),
        ("VIRPIL", "#ff6d00"),
    ]
    conn.executemany("INSERT INTO competitors (name, color) VALUES (?,?)", brands)


# ---------------------------------------------------------------------------
# Seed mock data
# ---------------------------------------------------------------------------
def seed_mock_data():
    with get_db() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0]
        if cnt > 0:
            return

    today = datetime.now()
    # Find last Monday
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7)
    
    mock_weeks = [
        {
            "week_start": (last_monday - timedelta(weeks=3)).strftime("%Y-%m-%d"),
            "week_end": (last_monday - timedelta(days=1) - timedelta(weeks=2)).strftime("%Y-%m-%d"),
            "overall_summary": "本周MOZA品牌整体舆情平稳，Reddit社区关于R5方向盘的讨论热度上升，主要集中在力反馈手感的正面评价。社交媒体提及量较前周增长8%。",
            "mention_count": 1240,
            "mention_change": 8.0,
            "positive_pct": 62.0,
            "neutral_pct": 28.0,
            "negative_pct": 10.0,
            "topics": [
                {"rank": 1, "topic": "MOZA R5 力反馈体验", "summary": "Reddit simracing社区多位用户称赞R5的DD直驱反馈细腻，性价比突出"},
                {"rank": 2, "topic": "MOZA 踏板模组升级", "summary": "Twitter和YouTube上有消息称MOZA即将发布新型负载踏板，用户期待值高"},
                {"rank": 3, "topic": "固件更新兼容性", "summary": "部分用户反馈最新固件与某些第三方软件存在兼容问题，官方已回应在修复中"},
            ],
            "risks": [
                {"level": "yellow", "content": "固件兼容性问题在小范围讨论，需关注后续发展", "source": "Reddit r/moza"},
            ],
            "sentiments": [
                {"sentiment": "positive", "viewpoint": "R5是目前入门级DD最佳选择之一", "source": "Reddit"},
                {"sentiment": "positive", "viewpoint": "方向盘材质和做工远超同价位竞品", "source": "YouTube评论"},
                {"sentiment": "negative", "viewpoint": "售后响应速度有待提升，等了三天才回复", "source": "Facebook用户群"},
            ],
            "actions": [
                "持续关注固件兼容性反馈，建议技术团队优先修复高优先级Bug",
                "针对R5正面口碑，可在社交媒体进行用户证言二次传播",
            ],
        },
        {
            "week_start": (last_monday - timedelta(weeks=2)).strftime("%Y-%m-%d"),
            "week_end": (last_monday - timedelta(days=1) - timedelta(weeks=1)).strftime("%Y-%m-%d"),
            "overall_summary": "本周MOZA舆情热度显著上升，主要驱动因素为SimRacing Expo展会上MOZA新品预告引发的讨论。整体情绪偏正面，但关于发货延迟的负面声音也有所增加。",
            "mention_count": 1580,
            "mention_change": 27.4,
            "positive_pct": 68.0,
            "neutral_pct": 20.0,
            "negative_pct": 12.0,
            "topics": [
                {"rank": 1, "topic": "SimRacing Expo 新品预告", "summary": "MOZA在展会亮相的新品引发热议，疑似新款直驱基座和F1方向盘"},
                {"rank": 2, "topic": "发货延迟投诉", "summary": "Instagram和Twitter上部分用户抱怨预订产品发货延迟，客服沟通不足"},
                {"rank": 3, "topic": "MOZA与Fanatec对比", "summary": "多个YouTuber发布MOZA vs Fanatec对比评测，MOZA生态性价比获认可"},
                {"rank": 4, "topic": "Pithouse软件更新", "summary": "最新版Pithouse增加了多项设置选项，社区反馈积极"},
            ],
            "risks": [
                {"level": "yellow", "content": "发货延迟投诉有所增加，建议加强物流信息透明度", "source": "Twitter/Instagram"},
                {"level": "green", "content": "新品预告热度高，需做好上市前的舆情引导准备", "source": "多渠道"},
            ],
            "sentiments": [
                {"sentiment": "positive", "viewpoint": "MOZA生态圈扩展速度惊人，已成为Fanatec最有力挑战者", "source": "YouTube评测"},
                {"sentiment": "negative", "viewpoint": "预定了两个月还没收到货，官方渠道信息不透明", "source": "Twitter"},
                {"sentiment": "negative", "viewpoint": "客服机器人回复敷衍，没有解决实际问题", "source": "Instagram DM"},
            ],
            "actions": [
                "建议物流部门在官网和社媒主动发布发货进度公告",
                "新品上市前准备FAQ和舆情应对预案",
            ],
        },
        {
            "week_start": (last_monday - timedelta(weeks=1)).strftime("%Y-%m-%d"),
            "week_end": (last_monday - timedelta(days=1)).strftime("%Y-%m-%d"),
            "overall_summary": "本周MOZA舆情整体热度小幅回落但保持稳定。产品质量口碑持续走强，但关于售后服务的负面讨论需要引起重视。Facebook用户群中出现大规模讨论，主要集中在配件兼容性和软件体验上。",
            "mention_count": 1420,
            "mention_change": -10.1,
            "positive_pct": 58.0,
            "neutral_pct": 27.0,
            "negative_pct": 15.0,
            "topics": [
                {"rank": 1, "topic": "售后服务体验", "summary": "多位用户分享售后经历，部分正面（快速换货）但也有负面（响应慢）"},
                {"rank": 2, "topic": "第三方配件兼容性", "summary": "Reddit和Facebook讨论方向盘支架、快拆等第三方配件的兼容性问题"},
                {"rank": 3, "topic": "F1 2026游戏适配", "summary": "F1 2026新游戏发布后，MOZA方向盘配置文件成为社区热门话题"},
                {"rank": 4, "topic": "新手入门指南", "summary": "YouTube上的MOZA新手设置教程获得高播放量，品牌教育内容受欢迎"},
                {"rank": 5, "topic": "Discord社区活动", "summary": "官方Discord举行了AMA活动，用户参与度高"},
            ],
            "risks": [
                {"level": "red", "content": "售后服务负面讨论占比上升至15%，需立即制定改善方案", "source": "Facebook/Reddit"},
                {"level": "yellow", "content": "第三方配件兼容性问题可能影响用户购买决策", "source": "Reddit/论坛"},
            ],
            "sentiments": [
                {"sentiment": "positive", "viewpoint": "AMA活动拉近了品牌与用户的距离，建议定期举办", "source": "Discord"},
                {"sentiment": "neutral", "viewpoint": "配件兼容性大多是第三方的问题，MOZA应该出官方指南", "source": "Reddit"},
                {"sentiment": "negative", "viewpoint": "售后体验两极分化严重，品控一致性需要加强", "source": "Facebook用户群"},
            ],
            "actions": [
                "重点优化售后服务流程，建议引入客服SLA考核机制",
                "发布官方第三方配件兼容性列表和使用指南",
                "在Discord保持每月至少一次AMA活动频率",
            ],
        },
    ]

    with get_db() as conn:
        for w in mock_weeks:
            cursor = conn.execute(
                """INSERT INTO weekly_reports 
                   (week_start, week_end, overall_summary, mention_count, 
                    mention_change, positive_pct, neutral_pct, negative_pct)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (w["week_start"], w["week_end"], w["overall_summary"],
                 w["mention_count"], w["mention_change"],
                 w["positive_pct"], w["neutral_pct"], w["negative_pct"])
            )
            rid = cursor.lastrowid

            for t in w["topics"]:
                conn.execute(
                    "INSERT INTO hot_topics (report_id, rank, topic, summary) VALUES (?,?,?,?)",
                    (rid, t["rank"], t["topic"], t["summary"])
                )

            for r in w["risks"]:
                conn.execute(
                    "INSERT INTO risk_alerts (report_id, level, content, source) VALUES (?,?,?,?)",
                    (rid, r["level"], r["content"], r["source"])
                )

            for s in w["sentiments"]:
                conn.execute(
                    "INSERT INTO sentiment_samples (report_id, sentiment, viewpoint, source) VALUES (?,?,?,?)",
                    (rid, s["sentiment"], s["viewpoint"], s["source"])
                )

            for a in w["actions"]:
                conn.execute(
                    "INSERT INTO action_items (report_id, action_text) VALUES (?,?)",
                    (rid, a if isinstance(a, str) else a.get("action_text", str(a)))
                )


def seed_competitor_mock_data():
    """Seed mock competitor tracking data."""
    today = datetime.now()
    days_since_monday = today.weekday()
    last_monday = today - timedelta(days=days_since_monday + 7)
    week_start = (last_monday - timedelta(weeks=1)).strftime("%Y-%m-%d")
    week_end = (last_monday - timedelta(days=1)).strftime("%Y-%m-%d")

    with get_db() as conn:
        # Init competitor list
        existing = conn.execute("SELECT COUNT(*) FROM competitors").fetchone()[0]
        if existing == 0:
            brands = [
                ("Fanatec", "#f5a623"),
                ("Simagic", "#00c2ff"),
                ("Thrustmaster", "#e53935"),
                ("PXN", "#7c4dff"),
                ("WINCTRL", "#00e676"),
                ("VIRPIL", "#ff6d00"),
            ]
            conn.executemany("INSERT INTO competitors (name, color) VALUES (?,?)", brands)

        cnt = conn.execute(
            "SELECT COUNT(*) FROM competitor_updates WHERE week_start=?",
            (week_start,)
        ).fetchone()[0]
        if cnt > 0:
            return

        competitors = {r["name"]: r["id"] for r in conn.execute("SELECT * FROM competitors").fetchall()}

        mock_data = {
            "Fanatec": [
                ("product_change", "发布 ClubSport DD+ 固件 v2.1.3，优化了力反馈算法降低延迟", "官网 changelog", "positive"),
                ("product_change", "F1 官方授权方向盘 CSL Elite Steering Wheel F1ESports V2 开启预售", "官网 / Twitter", "positive"),
                ("community_feedback", "Reddit 多位用户报告 DD Pro 基座高温降功率问题，官方尚未正式回应", "Reddit r/Fanatec", "negative"),
                ("business_movement", "母公司 Endor AG 发布 Q2 财报，营收同比下滑 12%，正寻求战略投资者", "财经媒体报道", "negative"),
            ],
            "Simagic": [
                ("product_change", "Alpha Mini 基座新固件支持 15Nm 峰值扭矩模式", "官方 Discord", "positive"),
                ("product_change", "P2000 踏板模组新增液压阻尼套件可选", "YouTube 评测", "positive"),
                ("community_feedback", "社区普遍好评，认为 Simagic 性价比正在超越 Fanatec 中端产品线", "Reddit / Bilibili", "positive"),
                ("business_movement", "签约美国最大 sim racing 零售商 Micro Center 全渠道铺货", "领英 / Twitter", "positive"),
            ],
            "Thrustmaster": [
                ("product_change", "T818 直驱基座新增 GT 风格方向盘和 F1 风格方向盘两款配件", "官网公告", "positive"),
                ("community_feedback", "T598 入门级 DD 产品线因缺货导致用户不满，官方称产能受限", "Twitter / Reddit", "negative"),
                ("community_feedback", "产品线过于碎片化，用户普遍建议精简 SKU", "Reddit 讨论", "neutral"),
                ("business_movement", "无重大商业动态，社交媒体活跃度持续下降", "多渠道", "neutral"),
            ],
            "PXN": [
                ("product_change", "V12 Lite 直驱方向盘正式发布，定价 ¥1,999 主打入门市场", "官网 / 京东", "positive"),
                ("product_change", "PXN 助手 App 3.0 上线，支持云端配置文件分享", "官博", "positive"),
                ("community_feedback", "B站多位UP主发布 V12 Lite 评测，总体评价偏正面但做工细节有待提升", "Bilibili", "neutral"),
                ("business_movement", "与京东达成战略合作，获选 2026 年度京东电竞外设重点扶持品牌", "新闻稿", "positive"),
            ],
            "WINCTRL": [
                ("product_change", "无公开产品更新信息", "多渠道监控", "neutral"),
                ("community_feedback", "Discord 社区规模较小但活跃度稳定，核心用户忠诚度高", "Discord", "neutral"),
                ("business_movement", "参加 ChinaJoy 2026 设立独立展台，展示完整生态产品线", "ChinaJoy 官方", "positive"),
            ],
            "VIRPIL": [
                ("product_change", "Constellation ALPHA Prime 摇杆开启预售，采用全金属结构", "官网 / Twitter", "positive"),
                ("community_feedback", "高端飞行模拟社区对 VIRPIL 新品评价极高，但价格争议持续", "r/hotas / DCS 论坛", "neutral"),
                ("business_movement", "与 DCS World 开发商 Eagle Dynamics 达成官方外设合作伙伴关系", "DCS 官方公告", "positive"),
            ],
        }

        for name, updates in mock_data.items():
            cid = competitors.get(name)
            if cid:
                for cat, content, source, sent in updates:
                    conn.execute(
                        """INSERT OR IGNORE INTO competitor_updates 
                           (competitor_id, week_start, week_end, category, content, source, sentiment)
                           VALUES (?,?,?,?,?,?,?)""",
                        (cid, week_start, week_end, cat, content, source, sent)
                    )

        conn.execute(
            """INSERT OR IGNORE INTO competitor_summaries (week_start, week_end, summary)
               VALUES (?,?,?)""",
            (week_start, week_end,
             "本周竞品市场分化明显：Simagic 凭借 Micro Center 渠道合作加速北美扩张，品牌势能持续上升；"
             "Fanatec 受母公司 Endor AG 财务危机拖累，社区信任度下滑；"
             "PXN 借助京东合作发力国内市场，V12 Lite 入门级 DD 定价激进；"
             "Thrustmaster 产品线碎片化和供货问题仍是主要挑战；"
             "VIRPIL 深耕高端飞行模拟细分赛道，DCS 合作提升专业品牌形象；"
             "WINCTRL 整体声量偏小但核心用户群体稳定。MOZA 作为直接竞品，需重点关注 Simagic 渠道扩张和 PXN 低价策略两条战线。")
        )


# ---------------------------------------------------------------------------
# Scheduled task: weekly data refresh
# ---------------------------------------------------------------------------
def weekly_refresh():
    """Called every Monday at 01:00 to check/refresh data."""
    with get_db() as conn:
        latest = conn.execute(
            "SELECT week_end FROM weekly_reports ORDER BY week_end DESC LIMIT 1"
        ).fetchone()
    if latest:
        app.logger.info(f"[Scheduler] Latest report week_end: {latest[0]}")


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.route("/api/reports")
def api_reports():
    with get_db() as conn:
        reports = conn.execute(
            "SELECT * FROM weekly_reports ORDER BY week_start DESC"
        ).fetchall()
    return jsonify([dict(r) for r in reports])


@app.route("/api/reports/<int:report_id>")
def api_report_detail(report_id):
    with get_db() as conn:
        report = conn.execute(
            "SELECT * FROM weekly_reports WHERE id=?", (report_id,)
        ).fetchone()
        if not report:
            return jsonify({"error": "Not found"}), 404

        topics = conn.execute(
            "SELECT * FROM hot_topics WHERE report_id=? ORDER BY rank", (report_id,)
        ).fetchall()
        risks = conn.execute(
            "SELECT * FROM risk_alerts WHERE report_id=?", (report_id,)
        ).fetchall()
        sentiments = conn.execute(
            "SELECT * FROM sentiment_samples WHERE report_id=?", (report_id,)
        ).fetchall()
        actions = conn.execute(
            "SELECT * FROM action_items WHERE report_id=?", (report_id,)
        ).fetchall()

    return jsonify({
        "report": dict(report),
        "topics": [dict(t) for t in topics],
        "risks": [dict(r) for r in risks],
        "sentiments": [dict(s) for s in sentiments],
        "actions": [dict(a) for a in actions],
    })


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """
    Ingest a weekly report. Accepts JSON matching the automation output.

    Expected JSON structure:
    {
        "week_start": "2026-07-06",
        "week_end": "2026-07-12",
        "overall_summary": "...",
        "mention_count": 1500,
        "mention_change": 5.5,
        "positive_pct": 65,
        "neutral_pct": 25,
        "negative_pct": 10,
        "topics": [
            {"rank": 1, "topic": "...", "summary": "..."}
        ],
        "risks": [
            {"level": "red/yellow/green", "content": "...", "source": "..."}
        ],
        "sentiments": [
            {"sentiment": "positive/neutral/negative", "viewpoint": "...", "source": "..."}
        ],
        "actions": [
            {"action_text": "..."}
        ]
    }
    """
    data = request.get_json(force=True)

    required = ["week_start", "week_end", "overall_summary"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM weekly_reports WHERE week_start=? AND week_end=?",
            (data["week_start"], data["week_end"])
        ).fetchone()

        if existing:
            # Update existing
            conn.execute("DELETE FROM hot_topics WHERE report_id=?", (existing["id"],))
            conn.execute("DELETE FROM risk_alerts WHERE report_id=?", (existing["id"],))
            conn.execute("DELETE FROM sentiment_samples WHERE report_id=?", (existing["id"],))
            conn.execute("DELETE FROM action_items WHERE report_id=?", (existing["id"],))
            conn.execute(
                """UPDATE weekly_reports SET 
                   overall_summary=?, mention_count=?, mention_change=?,
                   positive_pct=?, neutral_pct=?, negative_pct=?
                   WHERE id=?""",
                (data["overall_summary"], data.get("mention_count", 0),
                 data.get("mention_change", 0), data.get("positive_pct", 0),
                 data.get("neutral_pct", 0), data.get("negative_pct", 0),
                 existing["id"])
            )
            rid = existing["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO weekly_reports 
                   (week_start, week_end, overall_summary, mention_count,
                    mention_change, positive_pct, neutral_pct, negative_pct)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (data["week_start"], data["week_end"], data["overall_summary"],
                 data.get("mention_count", 0), data.get("mention_change", 0),
                 data.get("positive_pct", 0), data.get("neutral_pct", 0),
                 data.get("negative_pct", 0))
            )
            rid = cursor.lastrowid

        for t in data.get("topics", []):
            conn.execute(
                "INSERT INTO hot_topics (report_id, rank, topic, summary) VALUES (?,?,?,?)",
                (rid, t["rank"], t["topic"], t.get("summary", ""))
            )
        for r in data.get("risks", []):
            conn.execute(
                "INSERT INTO risk_alerts (report_id, level, content, source) VALUES (?,?,?,?)",
                (rid, r["level"], r["content"], r.get("source", ""))
            )
        for s in data.get("sentiments", []):
            conn.execute(
                "INSERT INTO sentiment_samples (report_id, sentiment, viewpoint, source) VALUES (?,?,?,?)",
                (rid, s["sentiment"], s["viewpoint"], s.get("source", ""))
            )
        for a in data.get("actions", []):
            conn.execute(
                "INSERT INTO action_items (report_id, action_text) VALUES (?,?)",
                (rid, a.get("action_text", a) if isinstance(a, dict) else a)
            )

    return jsonify({"status": "ok", "report_id": rid})


@app.route("/api/competitors/summary")
def api_competitor_summary():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM competitor_summaries ORDER BY week_end DESC LIMIT 1"
        ).fetchone()
        if not row:
            return jsonify({"summary": "暂无竞品动态数据"})
    return jsonify(dict(row))


@app.route("/api/competitors")
def api_competitors():
    with get_db() as conn:
        competitors = conn.execute("SELECT * FROM competitors ORDER BY name").fetchall()
        week_row = conn.execute(
            "SELECT week_start, week_end FROM competitor_summaries ORDER BY week_end DESC LIMIT 1"
        ).fetchone()

    result = []
    for c in competitors:
        updates = []
        if week_row:
            rows = get_db().execute(
                """SELECT * FROM competitor_updates 
                   WHERE competitor_id=? AND week_start=? AND week_end=?
                   ORDER BY 
                     CASE category 
                       WHEN 'product_change' THEN 1 
                       WHEN 'business_movement' THEN 2 
                       WHEN 'community_feedback' THEN 3 
                     END""",
                (c["id"], week_row["week_start"], week_row["week_end"])
            ).fetchall()
            updates = [dict(r) for r in rows]

        result.append({
            "id": c["id"],
            "name": c["name"],
            "color": c["color"],
            "week_start": week_row["week_start"] if week_row else None,
            "week_end": week_row["week_end"] if week_row else None,
            "updates": updates,
        })

    return jsonify(result)


@app.route("/api/competitors/ingest", methods=["POST"])
def api_competitor_ingest():
    """Ingest competitor tracking data."""
    data = request.get_json(force=True)
    required = ["week_start", "week_end", "competitors"]
    for field in required:
        if field not in data:
            return jsonify({"error": f"Missing field: {field}"}), 400

    with get_db() as conn:
        # Ensure competitors exist
        name_to_id = {}
        for r in conn.execute("SELECT * FROM competitors").fetchall():
            name_to_id[r["name"]] = r["id"]

        for comp in data["competitors"]:
            cname = comp.get("name", "")
            if cname not in name_to_id:
                cur = conn.execute("INSERT INTO competitors (name) VALUES (?)", (cname,))
                name_to_id[cname] = cur.lastrowid
            cid = name_to_id[cname]

            # Remove old updates for this week
            conn.execute(
                "DELETE FROM competitor_updates WHERE competitor_id=? AND week_start=? AND week_end=?",
                (cid, data["week_start"], data["week_end"])
            )

            for u in comp.get("updates", []):
                conn.execute(
                    """INSERT INTO competitor_updates 
                       (competitor_id, week_start, week_end, category, content, source, sentiment)
                       VALUES (?,?,?,?,?,?,?)""",
                    (cid, data["week_start"], data["week_end"],
                     u.get("category", "community_feedback"),
                     u.get("content", ""),
                     u.get("source", ""),
                     u.get("sentiment", "neutral"))
                )

        # Save/update summary
        if data.get("summary"):
            conn.execute(
                "DELETE FROM competitor_summaries WHERE week_start=? AND week_end=?",
                (data["week_start"], data["week_end"])
            )
            conn.execute(
                "INSERT INTO competitor_summaries (week_start, week_end, summary) VALUES (?,?,?)",
                (data["week_start"], data["week_end"], data["summary"])
            )

    return jsonify({"status": "ok", "competitors": len(data["competitors"])})


# ---------------------------------------------------------------------------
# Routes — Pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
init_db()
seed_mock_data()
seed_competitor_mock_data()

scheduler = BackgroundScheduler()
scheduler.add_job(weekly_refresh, "cron", day_of_week="mon", hour=1, minute=0,
                  id="weekly_refresh")
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
