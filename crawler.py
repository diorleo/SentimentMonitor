"""
Real data crawler for MOZA Sentiment Monitor.

Collects brand mention data from multiple sources:
  - Reddit (r/simracing + r/simracingdevices) via public JSON API
  - Google News via RSS feed
  - Bing News via RSS feed
  - Sim Racing news sites (TraxionGG, GTPlanet, SimRacing604) via RSS

Performs sentiment analysis (VADER if available, keyword-based fallback)
and formats data into the dashboard's expected structure.

All network calls have timeouts and graceful error handling.
If all sources fail, compile functions return None so app.py falls back to mock.
"""
import re
import time
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sentiment analysis
# ---------------------------------------------------------------------------
try:
    from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    _vader = SentimentIntensityAnalyzer()
except ImportError:
    _vader = None

# Fallback keyword lexicon (used only if vaderSentiment not installed)
_POSITIVE_WORDS = {
    "good", "great", "excellent", "amazing", "love", "best", "perfect",
    "awesome", "fantastic", "impressed", "recommend", "quality", "solid",
    "beautiful", "smooth", "responsive", "accurate", "premium", "value",
    "worth", "improve", "better", "win", "top", "leader", "impressive",
    "sturdy", "refined", "upgrade", "innovative", "competitive", "outstanding",
    "incredible", "superb", "remarkable", "flawless", "reliable",
}
_NEGATIVE_WORDS = {
    "bad", "terrible", "awful", "hate", "worst", "broken", "issue",
    "problem", "fail", "crash", "bug", "expensive", "overpriced", "cheap",
    "poor", "disappointing", "frustrating", "lag", "delay", "noise",
    "defect", "return", "refund", "complaint", "downside", "lack",
    "inferior", "unreliable", "mediocre", "subpar", "glitch", "wobble",
    "wobble", "flakey", "unacceptable", "scam", "ripoff",
}


def analyze_sentiment(text):
    """Return (label, compound_score).

    label is 'positive' | 'neutral' | 'negative'.
    Uses VADER if available, otherwise a keyword-based heuristic.
    """
    if not text or not text.strip():
        return "neutral", 0.0

    if _vader is not None:
        scores = _vader.polarity_scores(text)
        compound = scores["compound"]
        if compound >= 0.05:
            return "positive", compound
        if compound <= -0.05:
            return "negative", compound
        return "neutral", compound

    # Keyword fallback
    words = set(re.findall(r"\w+", text.lower()))
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
# Configuration
# ---------------------------------------------------------------------------
USER_AGENT = (
    "MOZA-Sentiment-Monitor/1.0 "
    "(sim racing brand monitoring; +https://sentimentmonitor.onrender.com)"
)
REQUEST_TIMEOUT = 15  # seconds per HTTP request
REDDIT_DELAY = 2      # seconds between Reddit API calls
NEWS_DELAY = 1        # seconds between Google News calls
MAX_PER_BRAND = 4     # max updates per competitor per week
TOTAL_BUDGET = 80     # max seconds for a full crawl (keeps Render startup under timeout)

REDDIT_SUBS = "simracing+simracingdevices"

# Sim Racing news sites — direct RSS feeds
# Entries may declare a "feed" (general feed, brand-filtered) and/or a
# "search_template" (server-side brand search, no extra filtering needed).
SIM_RACING_FEEDS = {
    "TraxionGG": {
        "feed": "https://traxion.gg/feed/",
        "search_template": "https://traxion.gg/?s={query}&feed=rss2",
    },
    "BoxThisLap": {
        "feed": "https://boxthislap.org/feed/",
        "search_template": "https://boxthislap.org/?s={query}&feed=rss2",
    },
    "SimRacingSetup": {
        "search_template": "https://www.simracingsetup.com/?s={query}&feed=rss2",
    },
    "iRacing": {
        "feed": "https://www.iracing.com/feed/",
    },
    "SimRacingExpo": {
        "feed": "https://www.simracingexpo.com/feed/",
    },
    "Studio397": {
        "feed": "https://forum.studio-397.com/index.php?forums/-/index.rss",
    },
    "GTPlanet": {
        "feed": "https://www.gtplanet.net/forum/forums/-/index.rss",
    },
    "SimRacing604": {
        "feed": "https://simracing604.com/feed/",
    },
}

# Search queries
MOZA_QUERY = "MOZA"
COMPETITOR_QUERIES = {
    "Fanatec": "Fanatec",
    "Simagic": "Simagic",
    "Thrustmaster": "Thrustmaster",
    "PXN": "PXN sim racing",
    "WINCTRL": "WINCTRL",
    "VIRPIL": "VIRPIL",
}

# Category keywords for competitor post classification
_PRODUCT_KEYWORDS = {
    "release", "launch", "new", "firmware", "update", "announce",
    "unveil", "introduce", "debut", "available", "pre-order", "ship",
    "preview", "reveal", "roadmap", "beta", "patch", "version",
}
_BUSINESS_KEYWORDS = {
    "partnership", "investment", "acquisition", "layoff", "revenue",
    "deal", "funding", "merge", "restructure", "hire", "expand",
    "collaboration", "sponsorship", "endorsement", "invest", "stock",
    "ceo", "executive", "financial", "quarterly", "earnings",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _strip_html(text):
    """Remove HTML tags from a string."""
    return re.sub(r"<[^>]+>", "", text).strip()


def _combined_text(item):
    """Extract combined text from a Reddit post or news item."""
    title = item.get("title", "")
    body = item.get("body", item.get("description", ""))
    return f"{title} {body}".strip()


def _truncate(text, max_len=200):
    """Truncate text to max_len characters, ending at a word boundary."""
    if len(text) <= max_len:
        return text
    cut = text[:max_len]
    # Try to break at a space
    last_space = cut.rfind(" ")
    if last_space > max_len - 50:
        cut = cut[:last_space]
    return cut + "..."


# ---------------------------------------------------------------------------
# Reddit crawler
# ---------------------------------------------------------------------------
def crawl_reddit(query, limit=25):
    """Search Reddit for posts matching *query* in the past week.

    Returns a list of dicts:
        {title, body, score, num_comments, created_utc, permalink, subreddit}
    """
    url = f"https://www.reddit.com/r/{REDDIT_SUBS}/search.json"
    params = {
        "q": query,
        "restrict_sr": 1,
        "sort": "new",
        "t": "week",
        "limit": limit,
    }
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code == 429:
            logger.warning("Reddit rate-limited (429) for query '%s'", query)
            return []
        resp.raise_for_status()
        data = resp.json()
        posts = []
        for child in data.get("data", {}).get("children", []):
            d = child.get("data", {})
            title = d.get("title", "")
            body = d.get("selftext", "")
            # Filter out posts that don't actually mention the query
            combined = (title + " " + body).lower()
            if query.lower() not in combined:
                continue
            posts.append({
                "title": title,
                "body": body,
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "created_utc": d.get("created_utc", 0),
                "permalink": d.get("permalink", ""),
                "subreddit": d.get("subreddit", "simracing"),
            })
        logger.info("Reddit: found %d posts for '%s'", len(posts), query)
        return posts
    except Exception as e:
        logger.warning("Reddit crawl failed for '%s': %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Google News crawler
# ---------------------------------------------------------------------------
def crawl_google_news(query, days=7):
    """Search Google News RSS for items matching *query* within the past *days*.

    Returns a list of dicts:
        {title, description, pub_date, source, link}
    """
    encoded = requests.utils.quote(query)
    url = (
        f"https://news.google.com/rss/search?q={encoded}"
        f"&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        cutoff = datetime.now() - timedelta(days=days)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "") or ""
            raw_desc = item.findtext("description", "") or ""
            desc = _strip_html(raw_desc)
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
        logger.info("Google News: found %d items for '%s'", len(items), query)
        return items
    except Exception as e:
        logger.warning("Google News crawl failed for '%s': %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Bing News crawler
# ---------------------------------------------------------------------------
def crawl_bing_news(query, days=7):
    """Search Bing News RSS for items matching *query* within the past *days*.

    Returns a list of dicts: {title, description, pub_date, source, link}
    """
    encoded = requests.utils.quote(query)
    url = f"https://www.bing.com/news/search?q={encoded}&format=rss"
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
        cutoff = datetime.now() - timedelta(days=days)
        items = []
        for item in root.findall(".//item"):
            title = item.findtext("title", "") or ""
            raw_desc = item.findtext("description", "") or ""
            desc = _strip_html(raw_desc)
            pub_date_str = item.findtext("pubDate", "") or ""
            source = item.findtext("source", "") or "Bing News"
            link = item.findtext("link", "") or ""

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
        logger.info("Bing News: found %d items for '%s'", len(items), query)
        return items
    except Exception as e:
        logger.warning("Bing News crawl failed for '%s': %s", query, e)
        return []


# ---------------------------------------------------------------------------
# Sim Racing news sites crawler
# ---------------------------------------------------------------------------
def crawl_sim_racing_sites(brand, days=7):
    """Fetch from sim racing news sites RSS feeds, filtered by brand keywords.

    Args:
        brand: Brand name (e.g., "MOZA", "Fanatec") for keyword filtering.
        days: Only include items from the past N days.

    Returns a list of dicts: {title, description, pub_date, source, link}
    """
    brand_keywords = [brand.lower()]
    if brand == "MOZA":
        brand_keywords.extend(["moza racing", "r5", "r9", "r12", "r16"])

    cutoff = datetime.now() - timedelta(days=days)
    all_items = []

    for site_name, feed_config in SIM_RACING_FEEDS.items():
        urls_to_try = []
        if "search_template" in feed_config:
            search_url = feed_config["search_template"].format(query=brand)
            urls_to_try.append((search_url, True))  # True = is search (no filter needed)
        if feed_config.get("feed"):
            urls_to_try.append((feed_config["feed"], False))  # general feed (needs filtering)

        for url, is_search in urls_to_try:
            try:
                resp = requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code != 200:
                    continue
                root = ET.fromstring(resp.content)
                for item in root.findall(".//item"):
                    title = item.findtext("title", "") or ""
                    raw_desc = item.findtext("description", "") or ""
                    desc = _strip_html(raw_desc)
                    pub_date_str = item.findtext("pubDate", "") or ""
                    source = site_name
                    link = item.findtext("link", "") or ""

                    # Brand keyword filter (only for general feeds, not search endpoints)
                    if not is_search:
                        combined = (title + " " + desc).lower()
                        if not any(kw in combined for kw in brand_keywords):
                            continue

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

                    all_items.append({
                        "title": title,
                        "description": desc,
                        "pub_date": pub_date_str,
                        "source": source,
                        "link": link,
                    })
                time.sleep(0.5)
            except Exception as e:
                logger.warning("%s crawl failed for '%s': %s", site_name, brand, e)

    logger.info("Sim Racing sites: found %d items for '%s'", len(all_items), brand)
    return all_items


# ---------------------------------------------------------------------------
# Data processing
# ---------------------------------------------------------------------------
def _extract_topics(reddit_posts, news_items, top_n=5):
    """Extract top topics, ranked by Reddit engagement then recency."""
    ranked = []
    for p in reddit_posts:
        engagement = p.get("score", 0) + p.get("num_comments", 0)
        ranked.append((engagement, p))
    for n in news_items:
        ranked.append((0, n))

    ranked.sort(key=lambda x: x[0], reverse=True)

    topics = []
    for i, (_, item) in enumerate(ranked[:top_n]):
        title = item.get("title", "")
        body = item.get("body", item.get("description", ""))
        summary = _truncate(body, 180) if body else _truncate(title, 180)
        topics.append({
            "rank": i + 1,
            "topic": _truncate(title, 120),
            "summary": summary,
        })
    return topics


def _extract_sentiment_samples(reddit_posts, news_items, top_n=3):
    """Pick the most strongly-worded posts as representative samples."""
    all_items = reddit_posts + news_items
    scored = []
    for item in all_items:
        text = _combined_text(item)
        label, score = analyze_sentiment(text)
        sub = item.get("subreddit", "")
        if sub:
            source = f"Reddit/r/{sub}"
        else:
            source = item.get("source", "Google News")
        scored.append((label, score, text, source))

    # Sort by absolute sentiment strength
    scored.sort(key=lambda x: abs(x[1]), reverse=True)

    # Try to include one of each label
    samples = []
    seen = set()
    for label, score, text, source in scored:
        if label not in seen:
            samples.append({
                "sentiment": label,
                "viewpoint": _truncate(text, 200),
                "source": source,
            })
            seen.add(label)
        if len(samples) >= top_n:
            break

    # Fill remaining
    for label, score, text, source in scored:
        if len(samples) >= top_n:
            break
        samples.append({
            "sentiment": label,
            "viewpoint": _truncate(text, 200),
            "source": source,
        })

    return samples


def _generate_risks(total, neg_pct, top_topics):
    """Generate risk alerts from sentiment distribution."""
    risks = []
    if total == 0:
        risks.append({
            "level": "green",
            "content": "本周未监测到显著讨论，社区活跃度较低",
            "source": "全渠道监控",
        })
        return risks

    if neg_pct >= 20:
        risks.append({
            "level": "red",
            "content": f"负面讨论占比达 {neg_pct}%，需立即关注并制定应对方案",
            "source": "Reddit/社区监控",
        })
    elif neg_pct >= 10:
        risks.append({
            "level": "yellow",
            "content": f"负面讨论占比 {neg_pct}%，建议持续关注社区反馈",
            "source": "Reddit/社区监控",
        })
    else:
        risks.append({
            "level": "green",
            "content": "本周舆情整体健康，负面讨论占比处于低位",
            "source": "全渠道监控",
        })
    return risks


def _generate_actions(pos_pct, neg_pct, top_topics, total):
    """Generate action items based on findings."""
    actions = []
    if total == 0:
        actions.append("本周社区讨论较少，建议主动发布内容提升品牌声量")
        actions.append("持续监测各渠道舆情动态")
        return actions

    if neg_pct >= 15 and top_topics:
        actions.append(
            f'针对"{top_topics[0]["topic"]}"相关负面反馈，'
            "建议产品团队跟进排查并回应用户关切"
        )
    if pos_pct >= 60 and top_topics:
        actions.append(
            f'利用"{top_topics[0]["topic"]}"的正面热度，'
            "加强社交媒体营销和内容传播"
        )
    if not actions:
        actions.append("持续监测社区舆情动态，保持品牌声量稳定")
    actions.append("定期与社区互动，收集用户反馈用于产品迭代")
    return actions


def _categorize_update(text):
    """Classify a post as product_change / business_movement / community_feedback."""
    words = set(re.findall(r"\w+", text.lower()))
    if words & _BUSINESS_KEYWORDS:
        return "business_movement"
    if words & _PRODUCT_KEYWORDS:
        return "product_change"
    return "community_feedback"


def _generate_moza_summary(mention_count, pos_pct, neu_pct, neg_pct, top_topics, sources):
    """Build the overall_summary text for the MOZA weekly report."""
    if mention_count == 0:
        return (
            "本周未监测到 MOZA 品牌相关讨论，社区活跃度较低。"
            "建议主动发布产品动态和社区内容以提升品牌声量。"
        )

    if pos_pct > 50:
        sentiment_desc = "偏向正面"
    elif neg_pct > 20:
        sentiment_desc = "偏向负面"
    else:
        sentiment_desc = "相对中性"

    source_desc = "、".join(sources) if sources else "多渠道"

    parts = [
        f"本周 MOZA 舆情共监测到 {mention_count} 条提及，"
        f"来源包括 {source_desc}。"
        f"整体情绪{sentiment_desc}，"
        f"正面占比 {pos_pct}%，中性占比 {neu_pct}%，负面占比 {neg_pct}%。",
    ]

    if top_topics:
        parts.append(f'本周最热门话题为"{top_topics[0]["topic"]}"。')

    if neg_pct > 20:
        parts.append("负面讨论占比较高，需重点关注用户反馈并制定改善方案。")
    elif pos_pct > 60:
        parts.append("社区整体反馈积极，品牌口碑良好。")
    else:
        parts.append("舆情整体平稳，建议持续关注社区动态。")

    return "".join(parts)


def _generate_competitor_summary(comp_data):
    """Build the weekly competitor summary paragraph."""
    if not comp_data:
        return "本周未监测到竞品重大动态。"

    parts = []
    for brand, updates in comp_data.items():
        if not updates:
            continue
        pos = sum(1 for _, _, _, s in updates if s == "positive")
        neg = sum(1 for _, _, _, s in updates if s == "negative")

        # Find the most notable update
        notable = updates[0][1][:80] if updates else ""

        if pos > neg:
            tone = "社区反馈偏正面"
        elif neg > pos:
            tone = "出现一些负面讨论"
        else:
            tone = "动态平稳"

        parts.append(f"{brand}本周{tone}" + (f"，主要动态：{notable}" if notable else ""))

    if not parts:
        return "本周未监测到竞品重大动态。"

    summary = "本周竞品动态：" + "；".join(parts) + "。"
    summary += "MOZA 需持续关注竞品产品发布和社区反馈，保持竞争力。"
    return summary


# ---------------------------------------------------------------------------
# Public API — called by app.py
# ---------------------------------------------------------------------------
def compile_moza_weekly_report(week_start, week_end):
    """Crawl real data from multiple sources and compile into the MOZA weekly report.

    Data sources: Reddit, Google News, Bing News, Sim Racing news sites.

    Returns a dict with keys matching generate_mock_week() variant structure,
    or None if no data was found from any source.
    """
    logger.info("[Crawler] Compiling MOZA report for %s ~ %s", week_start, week_end)

    all_posts = []
    all_news = []
    sources = set()
    crawl_start = time.time()

    # --- Reddit ---
    posts = crawl_reddit(MOZA_QUERY, limit=25)
    if posts:
        all_posts.extend(posts)
        sources.add("Reddit")
    time.sleep(REDDIT_DELAY)

    # --- Google News ---
    news = crawl_google_news("MOZA racing sim wheel", days=7)
    if news:
        all_news.extend(news)
        sources.add("Google News")
    time.sleep(NEWS_DELAY)

    # --- Bing News ---
    if time.time() - crawl_start < TOTAL_BUDGET:
        bing_news = crawl_bing_news("MOZA racing sim wheel", days=7)
        if bing_news:
            all_news.extend(bing_news)
            sources.add("Bing News")
        time.sleep(0.5)

    # --- Sim Racing news sites ---
    if time.time() - crawl_start < TOTAL_BUDGET:
        sr_news = crawl_sim_racing_sites("MOZA", days=7)
        if sr_news:
            all_news.extend(sr_news)
            for item in sr_news:
                sources.add(item.get("source", "Sim Racing News"))

    total_items = len(all_posts) + len(all_news)
    if total_items == 0:
        logger.warning("[Crawler] No data found from any source")
        return None

    # --- Sentiment analysis ---
    all_items = all_posts + all_news
    sentiments = [analyze_sentiment(_combined_text(item)) for item in all_items]

    total = len(sentiments)
    pos = sum(1 for s, _ in sentiments if s == "positive")
    neu = sum(1 for s, _ in sentiments if s == "neutral")
    neg = sum(1 for s, _ in sentiments if s == "negative")

    pos_pct = round(pos / total * 100) if total else 0
    neu_pct = round(neu / total * 100) if total else 0
    neg_pct = round(neg / total * 100) if total else 0

    # --- Structure ---
    topics = _extract_topics(all_posts, all_news)
    samples = _extract_sentiment_samples(all_posts, all_news)
    risks = _generate_risks(total, neg_pct, topics)
    actions = _generate_actions(pos_pct, neg_pct, topics, total)
    summary = _generate_moza_summary(total, pos_pct, neu_pct, neg_pct, topics, sorted(sources))

    logger.info(
        "[Crawler] MOZA report: %d mentions (%d pos / %d neu / %d neg)",
        total, pos, neu, neg,
    )

    return {
        "summary": summary,
        "mention_count": total,
        "change": 0,  # auto-calculated by /api/reports
        "pos": pos_pct,
        "neu": neu_pct,
        "neg": neg_pct,
        "topics": topics,
        "risks": risks,
        "sentiments": samples,
        "actions": actions,
    }


def compile_competitor_updates(week_start, week_end):
    """Crawl real data and compile into competitor updates format.

    Returns (data_dict, summary_string).
    data_dict maps brand name -> list of (category, content, source, sentiment).
    Returns (None, None) if no data was found at all.
    """
    logger.info("[Crawler] Compiling competitor data for %s ~ %s", week_start, week_end)
    crawl_start = time.time()

    result = {}

    for brand, query in COMPETITOR_QUERIES.items():
        # Check time budget
        elapsed = time.time() - crawl_start
        if elapsed > TOTAL_BUDGET:
            logger.warning("[Crawler] Time budget exceeded (%.1fs), stopping early", elapsed)
            result.setdefault(brand, [])
            break

        posts = crawl_reddit(query, limit=15)
        time.sleep(REDDIT_DELAY)

        news = crawl_google_news(f"{brand} sim racing", days=7)
        time.sleep(NEWS_DELAY)

        # Bing News
        bing_news = crawl_bing_news(f"{brand} sim racing", days=7)
        if bing_news:
            news.extend(bing_news)
        time.sleep(0.5)

        # Sim Racing news sites
        sr_news = crawl_sim_racing_sites(brand, days=7)
        if sr_news:
            news.extend(sr_news)

        all_items = posts + news

        if not all_items:
            result[brand] = []
            logger.info("[Crawler] %s: no data found", brand)
            continue

        # Process and categorize
        updates = []
        for item in all_items[:MAX_PER_BRAND]:
            text = _combined_text(item)
            if not text.strip():
                continue
            category = _categorize_update(text)
            label, _ = analyze_sentiment(text)

            sub = item.get("subreddit", "")
            if sub:
                source = f"Reddit/r/{sub}"
            else:
                source = item.get("source", "Google News")

            updates.append((category, _truncate(text, 200), source, label))

        result[brand] = updates
        logger.info("[Crawler] %s: %d updates", brand, len(updates))

    elapsed = time.time() - crawl_start
    logger.info("[Crawler] Competitor crawl finished in %.1fs", elapsed)

    # Check if we got any real data
    total_updates = sum(len(v) for v in result.values())
    if total_updates == 0:
        logger.warning("[Crawler] No competitor data found from any source")
        return None, None

    summary = _generate_competitor_summary(result)
    return result, summary
