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
                data_source   TEXT DEFAULT 'mock',
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
        # Migration: add data_source column if missing (for existing DBs)
        try:
            conn.execute("SELECT data_source FROM weekly_reports LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE weekly_reports ADD COLUMN data_source TEXT DEFAULT 'mock'")


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
# DB insertion helpers (shared by mock generator and real crawler)
# ---------------------------------------------------------------------------
def _insert_weekly_report(week_start_str, week_end_str, v):
    """Insert a weekly report and all related sub-records into the DB.
    *v* must have keys: summary, mention_count, change, pos, neu, neg,
    topics (list of {rank,topic,summary}), risks (list of {level,content,source}),
    sentiments (list of {sentiment,viewpoint,source}), actions (list of str).
    Returns the report ID.
    """
    with get_db() as conn:
        exists = conn.execute(
            "SELECT id FROM weekly_reports WHERE week_start=? AND week_end=?",
            (week_start_str, week_end_str)
        ).fetchone()
        if exists:
            return exists["id"]

        cursor = conn.execute(
            """INSERT INTO weekly_reports
               (week_start, week_end, overall_summary, mention_count,
                mention_change, positive_pct, neutral_pct, negative_pct, data_source)
               VALUES (?,?,?,?,?,?,?,?, 'mock')""",
            (week_start_str, week_end_str, v["summary"],
             v["mention_count"], v["change"],
             v["pos"], v["neu"], v["neg"])
        )
        rid = cursor.lastrowid

        for t in v["topics"]:
            conn.execute(
                "INSERT INTO hot_topics (report_id, rank, topic, summary) VALUES (?,?,?,?)",
                (rid, t["rank"], t["topic"], t["summary"])
            )
        for r in v["risks"]:
            conn.execute(
                "INSERT INTO risk_alerts (report_id, level, content, source) VALUES (?,?,?,?)",
                (rid, r["level"], r["content"], r["source"])
            )
        for s in v["sentiments"]:
            conn.execute(
                "INSERT INTO sentiment_samples (report_id, sentiment, viewpoint, source) VALUES (?,?,?,?)",
                (rid, s["sentiment"], s["viewpoint"], s["source"])
            )
        for a in v["actions"]:
            conn.execute(
                "INSERT INTO action_items (report_id, action_text) VALUES (?,?)",
                (rid, a)
            )
        return rid


def _insert_competitor_data(week_start, week_end, data_dict, summary):
    """Insert competitor updates and weekly summary into the DB.
    *data_dict* maps brand name -> list of (category, content, source, sentiment).
    """
    with get_db() as conn:
        competitors = {
            r["name"]: r["id"]
            for r in conn.execute("SELECT * FROM competitors").fetchall()
        }
        for name, updates in data_dict.items():
            cid = competitors.get(name)
            if not cid:
                continue
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
            (week_start, week_end, summary)
        )


# ---------------------------------------------------------------------------
# Seed mock data
# ---------------------------------------------------------------------------
def generate_mock_week(week_start_str, week_end_str, variant=0):
    """Generate a mock weekly report for the given week range.
    variant cycles through different mock content so each week looks different.
    """
    variants = [
        {
            "summary": "本周MOZA舆情整体热度小幅回落但保持稳定。产品质量口碑持续走强，但关于售后服务的负面讨论需要引起重视。Facebook用户群中出现大规模讨论，主要集中在配件兼容性和软件体验上。",
            "mention_count": 1420,
            "change": -10.1,
            "pos": 58, "neu": 27, "neg": 15,
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
        {
            "summary": "本周MOZA社交媒体声量显著上升，主要受新品R12直驱基座正式发布推动。Reddit和YouTube上的早期评测反馈积极，品牌热度持续走高。Discord社区新成员增长明显。",
            "mention_count": 1890,
            "change": 23.5,
            "pos": 72, "neu": 18, "neg": 10,
            "topics": [
                {"rank": 1, "topic": "R12直驱基座发布", "summary": "R12正式发布，12Nm扭矩、紧凑设计引发热议，早期评测反馈极佳"},
                {"rank": 2, "topic": "R12 vs Fanatec CSL DD", "summary": "多个YouTuber发布对比评测，MOZA R12在扭矩和价格上占优"},
                {"rank": 3, "topic": "Pithouse 2.0更新", "summary": "新版Pithouse软件界面重构，设置更直观，社区好评"},
                {"rank": 4, "topic": "SimRacing社区口碑", "summary": "r/simracing社区称MOZA已成为Fanatec最强替代方案"},
            ],
            "risks": [
                {"level": "yellow", "content": "R12初期产能有限，需关注供货和发货时效", "source": "官方社区"},
                {"level": "green", "content": "整体舆情健康，品牌热度处于上升通道", "source": "全渠道"},
            ],
            "sentiments": [
                {"sentiment": "positive", "viewpoint": "R12是目前中端DD市场最有竞争力的产品", "source": "YouTube评测"},
                {"sentiment": "positive", "viewpoint": "Pithouse 2.0终于把软件体验提升到了硬件水准", "source": "Reddit"},
                {"sentiment": "negative", "viewpoint": "希望MOZA能推出更多方向盘款式选择", "source": "Discord"},
            ],
            "actions": [
                "跟进R12评测内容，收集用户反馈用于产品迭代",
                "建议市场部利用R12热度开展社交媒体营销活动",
            ],
        },
        {
            "summary": "本周MOZA舆情整体平稳，社区讨论主要集中在产品生态扩展和固件更新上。SimRacing Expo相关传闻持续发酵，用户期待值较高。竞品动态活跃，Fanatec年底促销对市场格局产生扰动。",
            "mention_count": 1340,
            "change": -2.8,
            "pos": 64, "neu": 24, "neg": 12,
            "topics": [
                {"rank": 1, "topic": "固件v2.4更新", "summary": "最新固件优化了力反馈算法，降低了齿槽效应，用户反馈积极"},
                {"rank": 2, "topic": "MOZA生态扩展传闻", "summary": "据传MOZA正在开发手刹和序列档，社区讨论热烈"},
                {"rank": 3, "topic": "Fanatec促销冲击", "summary": "Fanatec黑色星期五大促对MOZA部分SKU形成价格压力"},
                {"rank": 4, "topic": "新手购机咨询", "summary": "Reddit上多次出现MOZA vs Thrustmaster入门选购咨询帖"},
            ],
            "risks": [
                {"level": "yellow", "content": "Fanatec促销可能短期影响MOZA入门级产品销量", "source": "市场分析"},
                {"level": "green", "content": "固件更新反馈积极，技术口碑稳步提升", "source": "Reddit/Discord"},
            ],
            "sentiments": [
                {"sentiment": "positive", "viewpoint": "固件更新频率和质量在行业里算顶尖的", "source": "Reddit"},
                {"sentiment": "positive", "viewpoint": "MOZA如果能出手刹就完美了，现在只能用第三方方案", "source": "Discord"},
                {"sentiment": "neutral", "viewpoint": "Fanatec打折确实香，但长远看MOZA生态更有潜力", "source": "Reddit"},
            ],
            "actions": [
                "密切关注Fanatec促销对市场份额的影响",
                "如手刹/序列档在开发中，可考虑在社区释放信号维持热度",
            ],
        },
    ]
    v = variants[variant % len(variants)]
    return _insert_weekly_report(week_start_str, week_end_str, v)


def get_current_week():
    """Return (week_start, week_end) for the most recently completed week."""
    today = datetime.now()
    # Most recent Sunday
    days_since_sunday = (today.weekday() + 1) % 7  # Monday=0, Sunday=6 → days since last Sunday
    if days_since_sunday == 0:
        days_since_sunday = 7
    last_sunday = today - timedelta(days=days_since_sunday)
    # Previous Monday to Sunday
    week_start = last_sunday - timedelta(days=6)
    return week_start.strftime("%Y-%m-%d"), last_sunday.strftime("%Y-%m-%d")


def ensure_current_week_data():
    """Auto-fill MOZA report + competitor data if current week is missing.
    Called on startup and by the scheduler — works even if server was asleep.

    Tries the real-data crawler first; falls back to mock on failure.
    """
    ws, we = get_current_week()
    with get_db() as conn:
        exists = conn.execute(
            "SELECT COUNT(*) FROM weekly_reports WHERE week_start=? AND week_end=?",
            (ws, we)
        ).fetchone()[0]
    if exists:
        return  # Already has data for this week

    # --- Try real crawler ---
    try:
        from crawler import compile_moza_weekly_report
        real_data = compile_moza_weekly_report(ws, we)
        if real_data:
            rid = _insert_weekly_report(ws, we, real_data)
            app.logger.info(f"[AutoFill] Crawled real MOZA report for {ws} ~ {we} (id={rid})")
            ensure_current_competitor_data(ws, we)
            return
        app.logger.warning("[Crawler] No real data found, falling back to mock")
    except Exception as e:
        app.logger.warning(f"[Crawler] MOZA crawl failed, falling back to mock: {e}")

    # --- Fallback: mock data ---
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0]
    rid = generate_mock_week(ws, we, variant=total)
    app.logger.info(f"[AutoFill] Generated mock MOZA report for {ws} ~ {we} (id={rid})")

    # Also fill competitor data
    ensure_current_competitor_data(ws, we)


def seed_mock_data():
    """Seed initial data if DB is completely empty, then fill current week."""
    with get_db() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM weekly_reports").fetchone()[0]
    if cnt == 0:
        # Generate one historical week so there's trend context
        today = datetime.now()
        days_since_monday = today.weekday()
        last_monday = today - timedelta(days=days_since_monday + 14)
        ws = last_monday.strftime("%Y-%m-%d")
        we = (last_monday + timedelta(days=6)).strftime("%Y-%m-%d")
        rid = generate_mock_week(ws, we, variant=0)
        app.logger.info(f"[Seed] Generated initial report for {ws} ~ {we} (id={rid})")

    # Always ensure current week has data
    ensure_current_week_data()


def get_competitor_mock_updates(variant=0):
    """Return mock competitor data. variant cycles through different content sets."""
    variants = [
        {
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
            "_summary": "本周竞品市场分化明显：Simagic 凭借 Micro Center 渠道合作加速北美扩张，品牌势能持续上升；"
                       "Fanatec 受母公司 Endor AG 财务危机拖累，社区信任度下滑；"
                       "PXN 借助京东合作发力国内市场，V12 Lite 入门级 DD 定价激进；"
                       "Thrustmaster 产品线碎片化和供货问题仍是主要挑战；"
                       "VIRPIL 深耕高端飞行模拟细分赛道，DCS 合作提升专业品牌形象；"
                       "WINCTRL 整体声量偏小但核心用户群体稳定。MOZA 作为直接竞品，需重点关注 Simagic 渠道扩张和 PXN 低价策略两条战线。",
        },
        {
            "Fanatec": [
                ("product_change", "DD Pro 推出散热底座配件套件，回应社区高温降功率反馈", "官网论坛", "neutral"),
                ("business_movement", "宣布进行组织架构调整，全球裁员 15% 以削减成本", "领英 / 财经媒体", "negative"),
                ("community_feedback", "裁员消息引发社区讨论，部分用户担忧售后和固件更新受影响", "Reddit", "negative"),
            ],
            "Simagic": [
                ("product_change", "发布全新 FX Pro 方向盘，碳纤维结构、4.3寸显示屏", "官网 / YouTube", "positive"),
                ("community_feedback", "FX Pro 定价 $699，社区认为性价比极高", "Reddit / Discord", "positive"),
                ("business_movement", "欧洲市场新增 5 家授权经销商，加速全球化布局", "领英", "positive"),
            ],
            "Thrustmaster": [
                ("product_change", "T-GT III 正式发布，支持 PS5/PC 双平台，零售价 ¥5,999", "官网 / 京东", "positive"),
                ("community_feedback", "T-GT III 定价偏高，社区期待降价", "Bilibili / Reddit", "neutral"),
                ("business_movement", "与 Gran Turismo 7 深度合作推出专属方向盘配置文件", "Twitter", "positive"),
            ],
            "PXN": [
                ("product_change", "V12 Lite 推出白色限定版，联名初音未来 IP", "京东 / B站", "positive"),
                ("community_feedback", "初音联名款引发二次元圈层关注，出圈效应明显", "B站 / 微博", "positive"),
                ("business_movement", "开启海外市场拓展，首批进入东南亚 3 国", "新闻稿", "positive"),
            ],
            "WINCTRL": [
                ("product_change", "发布首款直驱基座 WINDRIVE 10Nm，进入 DD 市场", "官网", "positive"),
                ("community_feedback", "WINDRIVE 软件体验被批评不够成熟，社区期待快速迭代", "Discord", "negative"),
            ],
            "VIRPIL": [
                ("product_change", "MongoosT-50CM4 油门发布，全金属 CNC 加工", "官网", "positive"),
                ("community_feedback", "飞行模拟社区一致好评，称做工和精度无可挑剔", "r/hotas", "positive"),
                ("business_movement", "在立陶宛扩建生产基地，产能提升 40%", "LinkedIn", "positive"),
            ],
            "_summary": "本周竞品格局发生显著变化：Fanatec 裁员消息冲击社区信心，品牌形象受损；"
                       "Simagic 凭借 FX Pro 方向盘巩固中高端地位，产品力持续提升；"
                       "PXN 通过初音未来 IP 联名实现破圈营销，在国内年轻用户群体中打开新局面；"
                       "WINCTRL 正式进军 DD 市场，WINDRIVE 首发但软件体验有待打磨；"
                       "Thrustmaster T-GT III 和 VIRPIL 新品各自巩固原有阵地。MOZA 需关注 Simagic 的中高端进攻和 PXN 的下沉破圈策略。",
        },
        {
            "Fanatec": [
                ("business_movement", "Endor AG 宣布获得 Corsair 战略投资，CEO 更换为 Corsair 高管", "财经媒体 / 领英", "positive"),
                ("product_change", "发布 Podium DD3 基座固件 v4.0，全面升级力反馈引擎，支持 25Nm 恒定扭矩", "官网 changelog", "positive"),
                ("community_feedback", "Corsair 入主后社区情绪明显回暖，订单交付周期从 6 周缩短至 2 周", "Reddit r/Fanatec", "positive"),
                ("community_feedback", "部分老用户担忧品牌定位变化，担心高品质定位被稀释", "Reddit / Twitter", "neutral"),
            ],
            "Simagic": [
                ("product_change", "推出 Alpha Ultimate 基座特别版，峰值扭矩 23Nm，定价 $1,199", "官网 / YouTube 评测", "positive"),
                ("community_feedback", "Reddit 对比评测: Alpha Ultimate vs Fanatec Podium DD3，Simagic 性价比胜出", "Reddit / Bilibili", "positive"),
                ("business_movement", "与 iRacing 合作举办 Simagic Cup 系列赛，奖金池 $50,000", "iRacing 官方公告", "positive"),
            ],
            "Thrustmaster": [
                ("product_change", "T-LCM PRO 踏板发布，全金属结构 + 液压阻尼升级", "官网 / Twitter", "positive"),
                ("community_feedback", "T598 供货恢复正常，入门级 DD 市场争夺白热化", "Reddit / Twitter", "neutral"),
                ("business_movement", "宣布与 Forza Motorsport 合作，成为 2026 年度官方推荐硬件", "Xbox Wire", "positive"),
            ],
            "PXN": [
                ("product_change", "V12 Pro 发布，12Nm 扭矩 + 快拆系统，定价 ¥2,999", "官网 / 京东 / B站", "positive"),
                ("community_feedback", "B站评测: V12 Pro 对标本竞品 R5/R9，做工和力反馈认可度高", "Bilibili", "positive"),
                ("business_movement", "海外市场拓展加速，入驻 Amazon US/UK/DE 三国站点", "领英 / 新闻稿", "positive"),
            ],
            "WINCTRL": [
                ("product_change", "WINDRIVE 软件 v2.1 发布，修复社区反馈的 FFB 延迟和 UI 卡顿问题", "Discord / 官网", "positive"),
                ("community_feedback", "软件更新后社区口碑明显改善，Discord 日活用户增长 40%", "Discord", "positive"),
            ],
            "VIRPIL": [
                ("product_change", "VPC Rotor TCS Plus 直升机总距控制器发布，填补市场空白", "官网 / r/hotas", "positive"),
                ("community_feedback", "DCS 和 MSFS 社区高度关注，称 VIRPIL 在专业飞行外设领域无可匹敌", "r/hotas / DCS 论坛", "positive"),
                ("business_movement", "新增日本和韩国经销商，亚太市场渠道覆盖扩大至 8 国", "领英", "positive"),
            ],
            "_summary": "本周竞品格局重大变化: Fanatec 获 Corsair 战略投资是 sim racing 行业年度最大新闻，"
                       "品牌信任度有望回升；Simagic 借 Alpha Ultimate 和 iRacing 赛事组合拳持续进攻高端市场；"
                       "Thrustmaster 获 Forza 官方合作加持；PXN 海外拓展提速，V12 Pro 定价对标一线品牌；"
                       "WINCTRL 软件体验改善后社区回暖；VIRPIL 在专业飞行模拟赛道继续巩固霸权。"
                       "MOZA 需密切关注 Corsair+Fanatec 组合可能带来的行业洗牌效应。",
        },
        {
            "Fanatec": [
                ("business_movement", "Corsair 整合后首次发布产品路线图，承诺 2026 Q3 前完成全产品线固件重构", "官方博客", "positive"),
                ("product_change", "BMW M4 GT3 官方方向盘宣发，定价 €1,499 为迄今最贵单品", "官网 / YouTube", "positive"),
                ("community_feedback", "高端方向盘定价引发争议，社区认为 Endor 在 Corsair 时代定价策略未变", "Reddit / Twitter", "neutral"),
            ],
            "Simagic": [
                ("product_change", "P2000-H 液压踏板套装正式上市，全 CNC 铝合金结构", "官网 / YouTube", "positive"),
                ("community_feedback", "P2000-H 被多位评测者称为\"千元级最强液压踏板\"", "Bilibili / Reddit", "positive"),
                ("business_movement", "签约欧洲最大 sim racing 连锁体验中心 RaceRoom，进驻 8 国 30+ 门店", "领英 / Twitter", "positive"),
            ],
            "Thrustmaster": [
                ("product_change", "推出入门级 DD 方向盘 T128 DD，定价 $199 直接对标 MOZA R3", "官网 / Twitter", "positive"),
                ("community_feedback", "T128 DD 定价激进，社区认为是 Thrustmaster 对 MOZA/PXN 下沉市场的反击", "Reddit / Bilibili", "positive"),
                ("community_feedback", "T818 用户抱怨配套方向盘选择太少，官方称 Q3 将扩展生态系统", "Twitter", "negative"),
            ],
            "PXN": [
                ("product_change", "PXN 推出赛车座椅 PS-01，配备 4 点式安全带和可调角度", "京东 / B站", "positive"),
                ("community_feedback", "PS-01 定价 ¥899 被赞\"国产性价比之王\"，首批秒罄", "Bilibili / 微博", "positive"),
                ("business_movement", "与腾讯 START 云游戏合作，PXN 设备支持云端即插即用", "新闻稿", "positive"),
            ],
            "WINCTRL": [
                ("business_movement", "启动 Pre-A 轮融资，估值目标 $50M，已获两家美元基金 TS", "36氪 / 投资界", "positive"),
                ("community_feedback", "融资消息在社区引发讨论，多数用户看好但担忧\"被大厂收购\"", "Discord", "neutral"),
            ],
            "VIRPIL": [
                ("product_change", "VPC WarBRD-D 摇杆底座升级至 v3 版本，支持双凸轮快调", "官网", "positive"),
                ("community_feedback", "WarBRD-D v3 被 DCS 玩家称为\"最适合直升机飞行的底座\"", "r/hotas", "positive"),
            ],
            "_summary": "本周行业聚焦中高端产品竞赛: Fanatec 发布 BMW M4 GT3 天价方向盘试探市场天花板；"
                       "Thrustmaster T128 DD 以 $199 超低价冲击入门市场，标志着 DD 普及化加速；"
                       "Simagic 在欧洲体验渠道大幅扩张，品牌渗透力增强；"
                       "PXN 向赛车座椅等外设周边延伸，生态闭环意图明显；"
                       "WINCTRL 融资信号显示资本对 sim racing 赛道信心；"
                       "MOZA 需警惕 Thrustmaster 低价策略对 R3/R5 产品线的直接冲击。",
        },
        {
            "Fanatec": [
                ("product_change", "ClubSport Handbrake V2 发布，终于加入线性和渐进式阻力调节", "官网 / YouTube", "positive"),
                ("community_feedback", "手刹产品线更新获拉力赛社区热烈反响，填补多年产品空白", "Reddit r/simracing", "positive"),
                ("business_movement", "Corsair 宣布将在 Computex 展示 Fanatec + Elgato 联动生态方案", "Corsair 官方", "positive"),
            ],
            "Simagic": [
                ("product_change", "发布 GT Neo X 方向盘，磁吸快拆 + 无线充电 + 5寸触摸屏", "官网 / YouTube 评测", "positive"),
                ("community_feedback", "GT Neo X 评测一致好评，用户称\"这是目前性价比最高的旗舰方向盘\"", "Reddit / Bilibili", "positive"),
                ("business_movement", "Q2 全球出货量同比增长 180%，北美市场份额首次超越 Thrustmaster", "行业分析报告", "positive"),
            ],
            "Thrustmaster": [
                ("product_change", "发布 eSwap X Racing 可换模块方向盘系统，支持赛车/卡车/方程式三种模式", "官网 / Gamescom", "positive"),
                ("community_feedback", "模块化概念受好评但首发固件 bug 较多，社区建议等 2.0 版本", "Reddit", "neutral"),
                ("business_movement", "宣布与 Renault Sport 合作，推出 Alpine F1 联名方向盘", "Twitter / 官网", "positive"),
            ],
            "PXN": [
                ("product_change", "PXN 生态 App 4.0 上线，新增 AI 驾驶分析 + 社区挑战赛功能", "官博 / B站", "positive"),
                ("community_feedback", "AI 驾驶分析功能被认可但数据准确性在复杂赛道场景中有待提升", "Bilibili", "neutral"),
                ("business_movement", "签约 LPL 知名电竞选手 Uzi 为品牌代言人，出圈营销力度加大", "微博 / 新闻稿", "positive"),
            ],
            "WINCTRL": [
                ("product_change", "WINDRIVE 方向盘发布碳纤维限量版，定价 $899 定位高端", "官网 / Twitter", "positive"),
                ("community_feedback", "碳纤维版做工获好评但软件生态仍是短板，与主流赛车游戏兼容性待完善", "Reddit / Discord", "neutral"),
            ],
            "VIRPIL": [
                ("product_change", "VPC ACE 系列座舱面板上市: 起飞/巡航/战斗三款模块化面板", "官网 / r/hotas", "positive"),
                ("community_feedback", "座舱面板填补了发烧友\"全物理座舱\"的最后一块拼图", "DCS 论坛 / Reddit", "positive"),
                ("business_movement", "与 MSFS 2026 深度集成，VIRPIL 设备全部支持即插即用识别", "微软官方 / VIRPIL", "positive"),
            ],
            "_summary": "本周竞品走势分化加剧: Simagic Q2 数据亮眼，北美市场份额突破标志着品牌质变；"
                       "Fanatec 在 Corsair 整合期稳步推出新品，手刹更新和 Computex 生态展示显示战略清晰化；"
                       "Thrustmaster 模组化方向盘和 Alpine F1 联名双双发力但首发品控需提升；"
                       "PXN 签约 Uzi 出圈营销 + AI 生态 App 构建长期用户粘性；"
                       "WINCTRL 高端化尝试碳纤维版但软件体验仍是核心短板；"
                       "VIRPIL 座舱面板生态化战略在飞行模拟赛道独占鳌头。"
                       "MOZA 需紧急关注 Simagic 北美增长态势，以及 Thrustmaster 模组化产品对中端市场的潜在分流。",
        },
    ]
    data = variants[variant % len(variants)]
    summary = data.pop("_summary", "")
    return data, summary


def ensure_current_competitor_data(week_start, week_end):
    """Auto-fill competitor data for the given week if missing.

    Tries the real-data crawler first; falls back to mock on failure.
    """
    with get_db() as conn:
        # Ensure competitor list exists
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

        # Check if this week's data already exists
        cnt = conn.execute(
            "SELECT COUNT(*) FROM competitor_updates WHERE week_start=? AND week_end=?",
            (week_start, week_end)
        ).fetchone()[0]
        if cnt > 0:
            return

        # Determine variant (for mock fallback)
        total = conn.execute("SELECT COUNT(DISTINCT week_start) FROM competitor_updates").fetchone()[0]

    # --- Try real crawler ---
    try:
        from crawler import compile_competitor_updates
        real_data, real_summary = compile_competitor_updates(week_start, week_end)
        if real_data is not None:
            _insert_competitor_data(week_start, week_end, real_data, real_summary)
            app.logger.info(f"[AutoFill] Crawled real competitor data for {week_start} ~ {week_end}")
            return
        app.logger.warning("[Crawler] No real competitor data found, falling back to mock")
    except Exception as e:
        app.logger.warning(f"[Crawler] Competitor crawl failed, falling back to mock: {e}")

    # --- Fallback: mock data ---
    mock_data, summary = get_competitor_mock_updates(variant=total)
    _insert_competitor_data(week_start, week_end, mock_data, summary)
    app.logger.info(f"[AutoFill] Generated mock competitor data for {week_start} ~ {week_end}")


def seed_competitor_mock_data():
    """Seed initial competitor data, then ensure current week is filled."""
    with get_db() as conn:
        cnt = conn.execute("SELECT COUNT(*) FROM competitor_updates").fetchone()[0]

    if cnt == 0:
        # Generate one historical week so there's trend context
        today = datetime.now()
        days_since_monday = today.weekday()
        last_monday = today - timedelta(days=days_since_monday + 14)
        ws = last_monday.strftime("%Y-%m-%d")
        we = (last_monday + timedelta(days=6)).strftime("%Y-%m-%d")
        ensure_current_competitor_data(ws, we)
        app.logger.info(f"[Seed] Generated initial competitor data for {ws} ~ {we}")

    ws, we = get_current_week()
    ensure_current_competitor_data(ws, we)


# ---------------------------------------------------------------------------
# Scheduled task: weekly data refresh
# ---------------------------------------------------------------------------
def weekly_refresh():
    """Called every Monday at 09:00 — auto-fill current week and clean old data."""
    # First, ensure current week has data
    ensure_current_week_data()

    with get_db() as conn:
        # Delete reports older than 8 weeks
        old_reports = conn.execute(
            "SELECT id FROM weekly_reports WHERE week_end < date('now', '-56 days')"
        ).fetchall()
        if old_reports:
            old_ids = [r["id"] for r in old_reports]
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM hot_topics WHERE report_id IN ({placeholders})", old_ids)
            conn.execute(f"DELETE FROM risk_alerts WHERE report_id IN ({placeholders})", old_ids)
            conn.execute(f"DELETE FROM sentiment_samples WHERE report_id IN ({placeholders})", old_ids)
            conn.execute(f"DELETE FROM action_items WHERE report_id IN ({placeholders})", old_ids)
            conn.execute(f"DELETE FROM weekly_reports WHERE id IN ({placeholders})", old_ids)
            app.logger.info(f"[Scheduler] Cleaned {len(old_ids)} old reports (older than 8 weeks)")

        # Delete old competitor updates (older than 8 weeks)
        conn.execute(
            "DELETE FROM competitor_updates WHERE week_end < date('now', '-56 days')"
        )
        conn.execute(
            "DELETE FROM competitor_summaries WHERE week_end < date('now', '-56 days')"
        )


# ---------------------------------------------------------------------------
# Routes — API
# ---------------------------------------------------------------------------
@app.route("/api/reports")
def api_reports():
    with get_db() as conn:
        reports = conn.execute(
            "SELECT * FROM weekly_reports ORDER BY week_start DESC"
        ).fetchall()
        # Auto-calculate mention_change only between real-data reports
        report_list = []
        for i, r in enumerate(reports):
            r_dict = dict(r)
            # Only calculate change if both current and previous are real data
            if i + 1 < len(reports):
                prev = reports[i + 1]
                if (r["data_source"] == "real" and prev["data_source"] == "real"
                        and prev["mention_count"] and prev["mention_count"] > 0):
                    r_dict["mention_change"] = round(
                        (r["mention_count"] - prev["mention_count"]) / prev["mention_count"] * 100, 1
                    )
                elif r["data_source"] == "real" and prev["data_source"] != "real":
                    r_dict["mention_change"] = 0  # Don't compare real vs mock
            report_list.append(r_dict)
    return jsonify(report_list)


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
                   positive_pct=?, neutral_pct=?, negative_pct=?, data_source='real'
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
                    mention_change, positive_pct, neutral_pct, negative_pct, data_source)
                   VALUES (?,?,?,?,?,?,?,?, 'real')""",
                (data["week_start"], data["week_end"], data["overall_summary"],
                 data.get("mention_count", 0), data.get("mention_change", 0),
                 data.get("positive_pct", 0), data.get("neutral_pct", 0),
                 data.get("negative_pct", 0))
            )
            rid = cursor.lastrowid

        # Delete all mock reports (keep only real data)
        mock_rows = conn.execute(
            "SELECT id FROM weekly_reports WHERE data_source='mock'"
        ).fetchall()
        for mr in mock_rows:
            conn.execute("DELETE FROM hot_topics WHERE report_id=?", (mr["id"],))
            conn.execute("DELETE FROM risk_alerts WHERE report_id=?", (mr["id"],))
            conn.execute("DELETE FROM sentiment_samples WHERE report_id=?", (mr["id"],))
            conn.execute("DELETE FROM action_items WHERE report_id=?", (mr["id"],))
            conn.execute("DELETE FROM weekly_reports WHERE id=?", (mr["id"],))
        if mock_rows:
            app.logger.info(f"[Ingest] Cleaned {len(mock_rows)} mock reports")

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
import threading

init_db()


def _bg_seed_data():
    """Run data seeding in a background thread so the web server starts
    listening immediately.  The crawler may take 30-60s on Render; running
    it synchronously would block gunicorn startup and cause health-check
    timeouts.
    """
    try:
        seed_mock_data()
        seed_competitor_mock_data()
    except Exception as e:
        app.logger.error(f"[Startup] Background seed failed: {e}")


threading.Thread(target=_bg_seed_data, daemon=True).start()

scheduler = BackgroundScheduler()
scheduler.add_job(weekly_refresh, "cron", day_of_week="mon", hour=9, minute=0,
                  id="weekly_refresh")
scheduler.start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
