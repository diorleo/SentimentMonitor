"""
BrowserAct-enhanced scraper for MOZA Sentiment Monitor.

Uses BrowserAct's stealth-extract to fetch Google News RSS feeds with
anti-bot bypass, parse them, and push real data to the Render dashboard.

Usage:
    python browser_act_scraper.py                    # Scrape and push to Render
    python browser_act_scraper.py --dry-run          # Scrape but don't push
    python browser_act_scraper.py --local            # Push to localhost:5000

Requires:
    - browser-act CLI installed and API key configured
    - vaderSentiment for sentiment analysis
    - requests for pushing data to Render

Architecture:
    Google News RSS → BrowserAct stealth-extract → parse XML → VADER sentiment
    → format as dashboard JSON → POST to Render /api/ingest + /api/competitors/ingest
"""
import json
import os
import re
import sys
import time
import logging
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from collections import defaultdict

import requests

# Try to import VADER for sentiment analysis
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except ImportError:
    _vader = None
    print("WARNING: vaderSentiment not installed, using keyword fallback")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
RENDER_URL = os.environ.get("RENDER_URL", "https://sentimentmonitor.onrender.com")
BROWSER_ACT_CMD = os.environ.get("BROWSER_ACT_CMD", "browser-act")
STEALTH_TIMEOUT = 60  # seconds per stealth-extract call
RENDER_WAIT = 3       # seconds to wait for JS rendering
PUSH_TIMEOUT = 30     # seconds for POST to Render

# Google News RSS search queries
MOZA_QUERIES = [
    "MOZA racing sim wheel",
    "MOZA Racing direct drive",
    "MOZA R5 R9 R12 R16",
]

COMPETITOR_QUERIES = {
    "Fanatec": ["Fanatec sim racing", "Fanatec Podium CSL DD"],
    "Simagic": ["Simagic sim racing", "Simagic Alpha Mini"],
    "Thrustmaster": ["Thrustmaster sim racing wheel", "Thrustmaster T300 TS-XW"],
    "PXN": ["PXN sim racing wheel", "PXN V10 V12 steering wheel"],
    "WINCTRL": ["WINCTRL sim racing", "WINDRIVE direct drive"],
    "VIRPIL": ["VIRPIL hotas joystick", "VPC Constellation WarBRD"],
}

# Category keywords
_PRODUCT_KEYWORDS = {
    "release", "launch", "new", "firmware", "update", "announce",
    "unveil", "introduce", "debut", "available", "pre-order", "ship",
    "preview", "reveal", "roadmap", "beta", "patch", "version",
    "upgraded", "pro", "ultra", "upgrade",
}
_BUSINESS_KEYWORDS = {
    "partnership", "investment", "acquisition", "layoff", "revenue",
    "deal", "funding", "merge", "restructure", "hire", "expand",
    "collaboration", "sponsorship", "endorsement", "invest", "stock",
    "ceo", "executive", "financial", "quarterly", "earnings", "partner",
    "renews", "official", "license",
}

# Fallback sentiment words
_POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "love", "best", "perfect",
    "awesome", "fantastic", "impressed", "recommend", "quality", "solid",
    "beautiful", "smooth", "responsive", "accurate", "premium", "value",
    "worth", "improve", "better", "win", "top", "leader", "impressive",
    "sturdy", "refined", "upgrade", "innovative", "competitive", "outstanding",
    "incredible", "superb", "remarkable", "flawless", "reliable",
    # Sim racing domain positives
    "partnership", "official", "licensed", "deal", "collaboration",
    "launch", "reveal", "debut", "announce", "expand", "growth",
    "breakthrough", "award", "pro", "ultra", "upgraded", "enhanced",
    "renews", "endorsement", "exclusive",
}
_NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "hate", "worst", "broken", "issue",
    "problem", "fail", "crash", "bug", "expensive", "overpriced", "cheap",
    "poor", "disappointing", "frustrating", "lag", "delay", "noise",
    "defect", "return", "refund", "complaint", "downside", "lack",
    "inferior", "unreliable", "mediocre", "subpar", "glitch", "wobble",
    "unacceptable", "scam", "ripoff",
    # Sim racing domain negatives
    "recall", "lawsuit", "bankrupt", "layoff", "controversy", "backlash",
    "shortage", "delay", "cancel", "discontinue", "obsolete", "outdated",
}


# ---------------------------------------------------------------------------
# Sentiment analysis
# ---------------------------------------------------------------------------
def analyze_sentiment(text):
    """Return (label, compound_score). Uses VADER if available.

    For news headlines (which VADER tends to classify as neutral),
    applies domain keyword boosting to detect sentimentally charged content.
    """
    if not text or not text.strip():
        return "neutral", 0.0

    text_lower = text.lower()
    words = set(re.findall(r"\w+", text_lower))

    if _vader is not None:
        scores = _vader.polarity_scores(text)
        compound = scores["compound"]

        # Domain keyword boost: news headlines are often neutral to VADER
        # but contain sentimentally meaningful business/product signals
        pos_domain = len(words & _POSITIVE_WORDS)
        neg_domain = len(words & _NEGATIVE_WORDS)

        if compound >= 0.05:
            return "positive", compound
        if compound <= -0.05:
            return "negative", compound

        # VADER says neutral — check domain keywords
        if pos_domain > neg_domain and pos_domain >= 2:
            return "positive", 0.3
        if neg_domain > pos_domain and neg_domain >= 2:
            return "negative", -0.3

        return "neutral", compound

    # Keyword fallback
    pos = len(words & _POSITIVE_WORDS)
    neg = len(words & _NEGATIVE_WORDS)
    total = pos + neg
    if total == 0:
        return "neutral", 0.0
    score = (pos - neg) / total
    if score > 0.15:
        return "positive", score
    if score < -0.15:
        return "negative", score
    return "neutral", score


# ---------------------------------------------------------------------------
# BrowserAct stealth-extract
# ---------------------------------------------------------------------------
def stealth_extract(url):
    """Use browser-act stealth-extract to fetch page content.

    Returns the raw content string, or None on failure.
    """
    env = os.environ.copy()
    env["UV_TOOL_DIR"] = env.get("UV_TOOL_DIR", "C:/Users/leo/.workbuddy/binaries/python/tools")

    cmd = [
        BROWSER_ACT_CMD,
        "stealth-extract",
        url,
        "--content-type", "markdown",
        "--timeout", str(STEALTH_TIMEOUT),
        "--render-wait", str(RENDER_WAIT),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=STEALTH_TIMEOUT + 30,
            env=env,
        )
        if result.returncode != 0:
            logger.warning("stealth-extract failed for %s: %s", url, result.stderr[:200])
            return None

        output = result.stdout.strip()
        if output.startswith("Error"):
            logger.warning("stealth-extract error for %s: %s", url, output[:200])
            return None

        return output

    except subprocess.TimeoutExpired:
        logger.warning("stealth-extract timed out for %s", url)
        return None
    except FileNotFoundError:
        logger.error("browser-act CLI not found. Install with: uv tool install browser-act-cli")
        return None
    except Exception as e:
        logger.warning("stealth-extract exception for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Google News RSS parsing
# ---------------------------------------------------------------------------
def _strip_html(text):
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _clean_text(text):
    """Clean HTML entities and whitespace from text."""
    if not text:
        return ""
    # Decode common HTML entities
    entities = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&apos;": "'",
        "&#39;": "'",
        "&mdash;": "—",
        "&ndash;": "–",
        "&hellip;": "…",
    }
    for entity, replacement in entities.items():
        text = text.replace(entity, replacement)
    # Decode numeric entities
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _clean_title(title):
    """Clean a news title: remove HTML entities and trailing source suffix."""
    title = _clean_text(title)
    # Remove trailing source pattern like " - games.gg" or " | BoxThisLap"
    title = re.sub(r"\s*[-|]\s*[A-Za-z0-9\s.]+$", "", title)
    return title.strip()


def _truncate(text, max_len=200):
    """Truncate text to max_len characters."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_space = cut.rfind(" ")
    if last_space > max_len - 50:
        cut = cut[:last_space]
    return cut + "..."


def parse_google_news_rss(rss_content, days=7):
    """Parse Google News RSS XML content into news items.

    Returns list of dicts: {title, description, pub_date, source, link}
    """
    if not rss_content:
        return []

    # stealth-extract may prepend text like "This XML file does not appear..."
    # Extract just the <rss>...</rss> portion
    rss_start = rss_content.find("<rss")
    if rss_start == -1:
        rss_start = rss_content.find("<?xml")
    if rss_start > 0:
        rss_content = rss_content[rss_start:]
    elif rss_start == -1:
        # Try to find <channel> as fallback
        ch_start = rss_content.find("<channel")
        if ch_start > 0:
            rss_content = "<?xml version=\"1.0\"?><root>" + rss_content[ch_start:] + "</root>"
        else:
            logger.warning("No RSS XML found in content (first 200 chars): %s", rss_content[:200])
            return []

    # Fix unescaped & characters in URLs (stealth-extract may un-escape entities)
    # Replace & that is NOT part of a valid entity (&amp; &lt; &gt; &quot; &apos; &#...)
    rss_content = re.sub(r'&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)', '&amp;', rss_content)

    try:
        root = ET.fromstring(rss_content)
    except ET.ParseError as e:
        logger.warning("Failed to parse RSS XML: %s (first 200 chars: %s)", e, rss_content[:200])
        return []

    cutoff = datetime.now() - timedelta(days=days)
    items = []

    for item in root.findall(".//item"):
        title = item.findtext("title", "") or ""
        raw_desc = item.findtext("description", "") or ""
        desc = _clean_text(_strip_html(raw_desc))
        title = _clean_text(title)
        pub_date_str = item.findtext("pubDate", "") or ""
        source = item.findtext("source", "") or "Google News"
        link = item.findtext("link", "") or ""

        # Date filter
        pub_date = None
        if pub_date_str:
            try:
                pub_date = parsedate_to_datetime(pub_date_str)
                if pub_date and pub_date.tzinfo:
                    pub_date = pub_date.replace(tzinfo=None)
            except Exception:
                pass
        if pub_date and pub_date < cutoff:
            continue

        items.append({
            "title": title,
            "description": desc,
            "pub_date": pub_date_str,
            "source": source,
            "link": link,
        })

    return items


def crawl_google_news_via_browseract(query, days=7):
    """Fetch Google News RSS via BrowserAct stealth-extract.

    Returns list of news items.
    """
    encoded = requests.utils.quote(query)
    url = (
        f"https://news.google.com/rss/search?q={encoded}"
        f"&hl=en-US&gl=US&ceid=US:en"
    )

    logger.info("Fetching Google News: %s", query)
    content = stealth_extract(url)

    if not content:
        logger.warning("No content from stealth-extract for: %s", query)
        return []

    items = parse_google_news_rss(content, days=days)
    logger.info("Got %d news items for '%s'", len(items), query)
    return items


# ---------------------------------------------------------------------------
# Categorization
# ---------------------------------------------------------------------------
def _categorize_update(text):
    """Classify as product_change / business_movement / community_feedback."""
    words = set(re.findall(r"\w+", text.lower()))
    if words & _BUSINESS_KEYWORDS:
        return "business_movement"
    if words & _PRODUCT_KEYWORDS:
        return "product_change"
    return "community_feedback"


# ---------------------------------------------------------------------------
# MOZA report compilation
# ---------------------------------------------------------------------------
def compile_moza_report(week_start, week_end):
    """Scrape Google News for MOZA and compile into weekly report format.

    Returns dict matching dashboard's expected format, or None.
    """
    logger.info("=== Compiling MOZA report for %s ~ %s ===", week_start, week_end)

    all_news = []
    seen_titles = set()

    for query in MOZA_QUERIES:
        items = crawl_google_news_via_browseract(query, days=7)
        for item in items:
            title = item["title"]
            if title not in seen_titles:
                seen_titles.add(title)
                all_news.append(item)
        time.sleep(1)  # brief pause between calls

    if not all_news:
        logger.warning("No MOZA news found from Google News")
        return None

    # Sort by date (newest first)
    all_news.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

    # Sentiment analysis
    sentiments = []
    for item in all_news:
        text = f"{item['title']} {item['description']}"
        label, score = analyze_sentiment(text)
        sentiments.append((label, score))

    total = len(all_news)
    pos = sum(1 for s, _ in sentiments if s == "positive")
    neu = sum(1 for s, _ in sentiments if s == "neutral")
    neg = sum(1 for s, _ in sentiments if s == "negative")

    pos_pct = round(pos / total * 100) if total else 0
    neu_pct = round(neu / total * 100) if total else 0
    neg_pct = round(neg / total * 100) if total else 0

    # Extract topics (top 5 by recency)
    topics = []
    for i, item in enumerate(all_news[:5]):
        clean_title = _clean_title(item["title"])
        topics.append({
            "rank": i + 1,
            "topic": _truncate(clean_title, 120),
            "summary": _truncate(item["description"] or clean_title, 180),
        })

    # Extract sentiment samples — deduplicate by content, pick diverse sentiments
    scored_items = []
    seen_texts = set()
    for item, (label, score) in zip(all_news, sentiments):
        text = _clean_text(f"{item['title']} {item['description']}")
        # Remove source suffix from text (e.g., "Article Title - Source Name")
        text = _clean_title(text)
        # Deduplicate by first 80 chars
        text_key = text[:80].lower()
        if text_key in seen_texts:
            continue
        seen_texts.add(text_key)
        scored_items.append((label, abs(score), text, item["source"]))

    scored_items.sort(key=lambda x: x[1], reverse=True)

    samples = []
    seen_labels = set()
    # First pass: pick one of each sentiment type
    for label, _, text, source in scored_items:
        if label not in seen_labels:
            samples.append({
                "sentiment": label,
                "viewpoint": _truncate(text, 200),
                "source": source,
            })
            seen_labels.add(label)
        if len(samples) >= 3:
            break
    # If we don't have 3 yet, fill with remaining items
    if len(samples) < 3:
        for label, _, text, source in scored_items:
            if len(samples) >= 3:
                break
            if any(s["viewpoint"][:80] == text[:80] for s in samples):
                continue
            samples.append({
                "sentiment": label,
                "viewpoint": _truncate(text, 200),
                "source": source,
            })

    # Generate risks
    risks = []
    if neg_pct >= 20:
        risks.append({
            "level": "red",
            "content": f"负面讨论占比达 {neg_pct}%，需立即关注并制定应对方案",
            "source": "Google News监控",
        })
    elif neg_pct >= 10:
        risks.append({
            "level": "yellow",
            "content": f"负面讨论占比 {neg_pct}%，建议持续关注社区反馈",
            "source": "Google News监控",
        })
    else:
        risks.append({
            "level": "green",
            "content": "本周舆情整体健康，负面讨论占比处于低位",
            "source": "全渠道监控",
        })

    # Generate actions (must be list of {"action_text": "..."} objects)
    actions = []
    if pos_pct >= 50 and topics:
        actions.append({"action_text": f'借助"{topics[0]["topic"]}"的正面热度，在社交媒体加大传播力度，扩大品牌声量'})
    if neg_pct >= 15 and topics:
        actions.append({"action_text": f'针对"{topics[0]["topic"]}"相关负面反馈，建议产品团队跟进并制定改善方案'})
    if not actions:
        # Generate data-driven actions based on actual topics
        if topics:
            actions.append({"action_text": f'关注"{topics[0]["topic"]}"相关讨论动态，评估对品牌口碑的潜在影响'})
        else:
            actions.append({"action_text": "持续监测社区舆情动态，保持品牌声量稳定"})
    # Add competitive context if competitor data is available
    if topics and any("Fanatec" in t["topic"] or "Simagic" in t["topic"] for t in topics):
        actions.append({"action_text": "关注竞品在相关话题中的动向，及时调整市场策略"})
    else:
        actions.append({"action_text": "定期与社区互动，收集用户反馈用于产品迭代"})

    # Generate summary
    if pos_pct > 50:
        sentiment_desc = "偏向正面"
    elif neg_pct > 20:
        sentiment_desc = "偏向负面"
    else:
        sentiment_desc = "相对中性"

    summary = (
        f"本周 MOZA 舆情共监测到 {total} 条提及，来源包括 Google News。"
        f"整体情绪{sentiment_desc}，"
        f"正面占比 {pos_pct}%，中性占比 {neu_pct}%，负面占比 {neg_pct}%。"
    )
    if topics:
        summary += f'本周最热门话题为"{topics[0]["topic"]}"。'
    if pos_pct > 60:
        summary += "社区整体反馈积极，品牌口碑良好。"
    elif neg_pct > 20:
        summary += "负面讨论占比较高，需重点关注用户反馈。"
    else:
        summary += "舆情整体平稳，建议持续关注社区动态。"

    logger.info("MOZA report: %d mentions (%d%% pos / %d%% neu / %d%% neg)",
                total, pos_pct, neu_pct, neg_pct)

    return {
        "week_start": week_start,
        "week_end": week_end,
        "overall_summary": summary,
        "mention_count": total,
        "mention_change": 0,
        "positive_pct": pos_pct,
        "neutral_pct": neu_pct,
        "negative_pct": neg_pct,
        "topics": topics,
        "risks": risks,
        "sentiments": samples,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# Competitor data compilation
# ---------------------------------------------------------------------------
def compile_competitor_data(week_start, week_end):
    """Scrape Google News for competitors and compile into updates format.

    Returns (data_dict, summary_string).
    """
    logger.info("=== Compiling competitor data for %s ~ %s ===", week_start, week_end)

    result = {}

    for brand, queries in COMPETITOR_QUERIES.items():
        all_items = []
        seen_titles = set()

        for query in queries:
            items = crawl_google_news_via_browseract(query, days=7)
            for item in items:
                if item["title"] not in seen_titles:
                    seen_titles.add(item["title"])
                    all_items.append(item)
            time.sleep(1)

        if not all_items:
            result[brand] = []
            logger.info("%s: no news found", brand)
            continue

        # Sort by date
        all_items.sort(key=lambda x: x.get("pub_date", ""), reverse=True)

        # Process top items
        updates = []
        seen_content = set()
        for item in all_items[:4]:  # max 4 per brand
            text = _clean_text(f"{item['title']} {item['description']}")
            # Remove source suffix from text
            text = _clean_title(text)
            if not text.strip():
                continue
            # Deduplicate by first 80 chars
            content_key = text[:80].lower()
            if content_key in seen_content:
                continue
            seen_content.add(content_key)
            category = _categorize_update(text)
            label, _ = analyze_sentiment(text)
            source = item.get("source", "Google News")
            updates.append((category, _truncate(text, 200), source, label))

        result[brand] = updates
        logger.info("%s: %d updates", brand, len(updates))

    # Generate summary
    total_updates = sum(len(v) for v in result.values())
    if total_updates == 0:
        return None, None

    parts = []
    for brand, updates in result.items():
        if not updates:
            parts.append(f"{brand}本周暂无显著公开动态")
            continue
        pos = sum(1 for _, _, _, s in updates if s == "positive")
        neg = sum(1 for _, _, _, s in updates if s == "negative")
        notable = _clean_text(updates[0][1])[:80] if updates else ""

        if pos > neg:
            tone = "社区反馈偏正面"
        elif neg > pos:
            tone = "出现一些负面讨论"
        else:
            tone = "动态平稳"

        parts.append(f"{brand}本周{tone}" + (f"，主要动态：{notable}" if notable else ""))

    summary = "本周竞品动态：" + "；".join(parts) + "。"
    summary += "MOZA 需持续关注竞品产品发布和社区反馈，保持竞争力。"

    return result, summary


# ---------------------------------------------------------------------------
# Push to Render
# ---------------------------------------------------------------------------
def push_moza_report(base_url, report):
    """POST MOZA report to Render /api/ingest."""
    url = f"{base_url}/api/ingest"
    try:
        resp = requests.post(url, json=report, timeout=PUSH_TIMEOUT)
        if resp.status_code == 200:
            logger.info("MOZA report pushed successfully to %s", url)
            return True
        else:
            logger.warning("Push failed: %d %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.warning("Push failed: %s", e)
        return False


def push_competitor_data(base_url, week_start, week_end, comp_data, summary):
    """POST competitor data to Render /api/competitors/ingest.

    Expected format:
    {
        "week_start": "...",
        "week_end": "...",
        "summary": "...",
        "competitors": [
            {"name": "Fanatec", "updates": [{"category":"...","content":"...","source":"...","sentiment":"..."}]}
        ]
    }
    """
    url = f"{base_url}/api/competitors/ingest"
    competitors_list = []
    for brand, updates in comp_data.items():
        competitors_list.append({
            "name": brand,
            "updates": [
                {
                    "category": cat,
                    "content": content,
                    "source": src,
                    "sentiment": sent,
                }
                for cat, content, src, sent in updates
            ]
        })

    payload = {
        "week_start": week_start,
        "week_end": week_end,
        "summary": summary,
        "competitors": competitors_list,
    }

    try:
        resp = requests.post(url, json=payload, timeout=PUSH_TIMEOUT)
        if resp.status_code == 200:
            logger.info("Competitor data pushed successfully to %s", url)
            return True
        else:
            logger.warning("Push failed: %d %s", resp.status_code, resp.text[:200])
            return False
    except Exception as e:
        logger.warning("Push failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def get_current_week():
    """Get current week's Monday and Sunday dates as YYYY-MM-DD strings."""
    today = datetime.now()
    days_since_monday = today.weekday()
    monday = today - timedelta(days=days_since_monday)
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="BrowserAct-enhanced scraper for MOZA Sentiment Monitor")
    parser.add_argument("--dry-run", action="store_true", help="Scrape but don't push to Render")
    parser.add_argument("--local", action="store_true", help="Push to localhost:5000 instead of Render")
    parser.add_argument("--moza-only", action="store_true", help="Only scrape MOZA data")
    parser.add_argument("--competitors-only", action="store_true", help="Only scrape competitor data")
    args = parser.parse_args()

    base_url = "http://localhost:5000" if args.local else RENDER_URL
    logger.info("Target: %s", base_url)

    ws, we = get_current_week()
    logger.info("Week: %s ~ %s", ws, we)

    # Scrape MOZA data
    if not args.competitors_only:
        report = compile_moza_report(ws, we)
        if report:
            logger.info("MOZA report compiled successfully")
            if args.dry_run:
                logger.info("[Dry run] Would push MOZA report:\n%s",
                            json.dumps(report, indent=2, ensure_ascii=False)[:500])
            else:
                push_moza_report(base_url, report)
        else:
            logger.warning("No MOZA data found")

    # Scrape competitor data
    if not args.moza_only:
        comp_data, summary = compile_competitor_data(ws, we)
        if comp_data:
            logger.info("Competitor data compiled successfully")
            if args.dry_run:
                logger.info("[Dry run] Would push competitor data")
                for brand, updates in comp_data.items():
                    logger.info("  %s: %d updates", brand, len(updates))
            else:
                push_competitor_data(base_url, ws, we, comp_data, summary)
        else:
            logger.warning("No competitor data found")

    logger.info("Done!")


if __name__ == "__main__":
    main()
