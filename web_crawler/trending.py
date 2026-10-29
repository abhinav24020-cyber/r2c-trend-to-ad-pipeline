"""
trending.py — Trend-first sources, mirroring the R2C social engine's source list.

Phase 1, the trend radar (cheap; a handful of requests):
    Google Trends "trending now" RSS   -> records + queries
    Bluesky trending topics            -> queries
    Mastodon trending hashtags         -> queries
    Hugging Face trending models       -> records + queries

Only AI-relevant trends survive (crawler.is_relevant), and they become search
queries for phase 2 so the deep crawl follows what is trending right now.

Phase 2 sources added here (alongside crawler.py's HN / News / Mastodon / feeds):
    Bluesky search (top posts per query) and the "What's Hot" feed
    Hugging Face daily papers (community-upvoted research)
    StackExchange hot questions (+ one batched user lookup per site)
    YouTube search (only with YOUTUBE_API_KEY)

Not covered, by design: Twitter/X (Nitter mirrors are gone; the official API
is paid), LinkedIn and Quora (login walls; their terms forbid scraping).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List
from urllib.parse import quote, quote_plus

from bs4 import BeautifulSoup
from crawlee import Request
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

from trend_pipeline.text_utils import strip_html

from . import settings

# Request labels
GTRENDS = "GTRENDS"
BSKY_TOPICS = "BSKY_TOPICS"
MASTODON_TRENDING_TAGS = "MASTODON_TRENDING_TAGS"
HF_MODELS = "HF_MODELS"
HF_README = "HF_README"
HF_PAPERS = "HF_PAPERS"
BSKY_POSTS = "BSKY_POSTS"
SE_QUESTIONS = "SE_QUESTIONS"
SE_USERS = "SE_USERS"
YT_SEARCH = "YT_SEARCH"
YT_VIDEOS = "YT_VIDEOS"
YT_CHANNELS = "YT_CHANNELS"

_FRONT_MATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)
_MARKDOWN_NOISE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]+>|[#>*`|]+")
_TRAILING_VERSION_RE = re.compile(r"\b\d{4}\b$")


def model_query(model_id: str) -> str:
    """'deepseek-ai/DeepSeek-V4.1-Flash-1017' -> 'DeepSeek V4.1 Flash'."""
    name = model_id.split("/", 1)[-1]
    name = re.sub(r"[-_]+", " ", name).strip()
    return _TRAILING_VERSION_RE.sub("", name).strip()


def readme_text(markdown: str) -> str:
    text = _FRONT_MATTER_RE.sub("", markdown or "")
    text = _MARKDOWN_NOISE_RE.sub(" ", text)
    return " ".join(text.split())


def bluesky_url(post: Dict[str, Any]) -> str:
    handle = (post.get("author") or {}).get("handle", "")
    rkey = (post.get("uri") or "").rsplit("/", 1)[-1]
    return "https://bsky.app/profile/{}/post/{}".format(handle, rkey) if handle and rkey else ""


def traffic_to_int(value: str) -> int:
    """Google Trends approx_traffic: '200+', '10K+', '2M+' -> int."""
    match = re.match(r"\s*([\d.,]+)\s*([KkMm]?)", value or "")
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    return int(number * {"k": 1_000, "m": 1_000_000}.get(match.group(2).lower(), 1))


# ---------------------------------------------------------------------------
# Seed requests
# ---------------------------------------------------------------------------
def radar_seed_requests() -> List[Request]:
    seeds = [
        Request.from_url(
            "https://trends.google.com/trending/rss?geo={}".format(geo),
            label=GTRENDS, user_data={"geo": geo})
        for geo in settings.GOOGLE_TRENDS_GEOS
    ]
    seeds.append(Request.from_url(
        settings.BLUESKY_PUBLIC_API + "/xrpc/app.bsky.unspecced.getTrendingTopics",
        label=BSKY_TOPICS))
    seeds.append(Request.from_url(
        settings.MASTODON_INSTANCE.rstrip("/") + "/api/v1/trends/tags?limit=20",
        label=MASTODON_TRENDING_TAGS))
    seeds.append(Request.from_url(
        "https://huggingface.co/api/models?sort=trendingScore&limit={}".format(
            settings.HF_TRENDING_MODELS),
        label=HF_MODELS))
    return seeds


def deep_seed_requests(queries: Iterable[str]) -> List[Request]:
    queries = list(queries)
    seeds = [
        Request.from_url(
            "https://huggingface.co/api/daily_papers?limit={}".format(settings.HF_DAILY_PAPERS),
            label=HF_PAPERS),
        Request.from_url(
            settings.BLUESKY_PUBLIC_API + "/xrpc/app.bsky.feed.getFeed?feed={}&limit=50".format(
                quote(settings.BLUESKY_WHATS_HOT, safe="")),
            label=BSKY_POSTS, user_data={"query": "whats-hot", "filter_relevance": True}),
    ]
    for query in queries:
        seeds.append(Request.from_url(
            settings.BLUESKY_SEARCH_API + "/xrpc/app.bsky.feed.searchPosts"
            "?q={}&sort=top&lang=en&limit=25".format(quote_plus(query)),
            label=BSKY_POSTS, user_data={"query": query}))
    for site, tag in settings.STACKEXCHANGE_QUERIES:
        seeds.append(Request.from_url(
            "https://api.stackexchange.com/2.3/questions?order=desc&sort=hot&site={}"
            "&pagesize={}&filter=withbody{}".format(
                site, settings.MAX_ITEMS_PER_SOURCE_QUERY,
                "&tagged=" + quote(tag) if tag else ""),
            label=SE_QUESTIONS, user_data={"site": site}))
    if settings.YOUTUBE_API_KEY:
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=settings.MAX_ITEM_AGE_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for query in queries[: settings.YOUTUBE_MAX_QUERIES]:
            seeds.append(Request.from_url(
                "https://www.googleapis.com/youtube/v3/search?part=id&type=video&order=viewCount"
                "&relevanceLanguage=en&maxResults=10&publishedAfter={}&q={}".format(
                    since, quote_plus(query)),
                label=YT_SEARCH, headers={"X-goog-api-key": settings.YOUTUBE_API_KEY},
                user_data={"query": query}))
    return seeds


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
def register(
    crawler: BeautifulSoupCrawler,
    result: Any,
    now: datetime,
    is_relevant: Callable[..., bool],
    is_fresh: Callable[..., bool],
    to_iso: Callable[[Any], Any],
) -> None:
    """Attach the trend-radar and trend-source handlers to the shared crawler."""
    router = crawler.router

    def add_query(query: str, origin: str) -> None:
        query = " ".join((query or "").split())
        if query and query.lower() not in {q.lower() for q, _ in result.trending_queries}:
            result.trending_queries.append((query, origin))

    async def read_json(context: BeautifulSoupCrawlingContext) -> Any:
        return json.loads(await context.http_response.read())

    # -- phase 1: trend radar -------------------------------------------------
    @router.handler(GTRENDS)
    async def google_trends(context: BeautifulSoupCrawlingContext) -> None:
        xml = BeautifulSoup(await context.http_response.read(), "xml")
        geo = context.request.user_data.get("geo")
        for item in xml.find_all("item"):
            term = item.find("title").get_text(strip=True) if item.find("title") else ""
            news = [{
                "title": n.find("news_item_title").get_text(" ", strip=True) if n.find("news_item_title") else "",
                "url": n.find("news_item_url").get_text(strip=True) if n.find("news_item_url") else "",
                "source": n.find("news_item_source").get_text(strip=True) if n.find("news_item_source") else "",
            } for n in item.find_all("news_item")]
            if not is_relevant(term, *(n["title"] for n in news)):
                continue
            published = item.find("pubDate").get_text(strip=True) if item.find("pubDate") else ""
            if not is_fresh(published, now):
                continue
            traffic = item.find("approx_traffic")
            key = "{}|{}".format(geo, term.lower())
            if result.put("Google Trends", key, {
                "id": key,
                "query": term,
                "geo": geo,
                "approx_traffic": traffic_to_int(traffic.get_text() if traffic else ""),
                "published_at": to_iso(published),
                "news": news,
            }):
                add_query(term, "Google Trends " + geo)

    @router.handler(BSKY_TOPICS)
    async def bluesky_topics(context: BeautifulSoupCrawlingContext) -> None:
        data = await read_json(context)
        for topic in (data.get("topics") or []) + (data.get("suggested") or []):
            name = topic.get("displayName") or topic.get("topic") or ""
            if is_relevant(name, topic.get("description")):
                add_query(name, "Bluesky trending")

    @router.handler(MASTODON_TRENDING_TAGS)
    async def mastodon_trending_tags(context: BeautifulSoupCrawlingContext) -> None:
        for tag in await read_json(context) or []:
            # Hashtags are run together ("GenAI"); split camel case before matching.
            name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tag.get("name", ""))
            if is_relevant(name):
                add_query(name, "Mastodon trending")

    @router.handler(HF_MODELS)
    async def hf_models(context: BeautifulSoupCrawlingContext) -> None:
        for model in await read_json(context) or []:
            model_id = model.get("id", "")
            if not model_id:
                continue
            # Trending now is a current signal even for an older model, so it
            # always seeds a query; only a fresh release becomes a record.
            add_query(model_query(model_id), "Hugging Face trending")
            if not is_fresh(model.get("createdAt"), now):
                continue
            # Readable name in the title: Gate 1 corroboration matches words, and
            # 'deepseek-ai/DeepSeek-V4.1-Flash-1017' never matches 'DeepSeek V4.1 Flash'.
            record = {
                "id": model_id,
                "kind": "model",
                "title": "{} by {} is trending on Hugging Face".format(
                    model_query(model_id), model.get("author") or model_id.split("/")[0]),
                "summary": "Trending {} model{} by {}. {} likes, {} downloads.".format(
                    model.get("pipeline_tag") or "open",
                    " ({})".format(model["library_name"]) if model.get("library_name") else "",
                    model.get("author") or model_id.split("/")[0],
                    model.get("likes", 0), model.get("downloads", 0)),
                "url": "https://huggingface.co/" + model_id,
                "author": model.get("author") or model_id.split("/")[0],
                "created_at": model.get("createdAt"),
                "likes": model.get("likes", 0),
                "downloads": model.get("downloads", 0),
                "trending_score": model.get("trendingScore", 0),
            }
            if result.put("Hugging Face", model_id, record):
                if not model.get("gated"):
                    await context.add_requests([Request.from_url(
                        "https://huggingface.co/{}/raw/main/README.md".format(model_id),
                        label=HF_README, user_data={"key": model_id})])

    @router.handler(HF_README)
    async def hf_readme(context: BeautifulSoupCrawlingContext) -> None:
        record = result.get("Hugging Face", context.request.user_data["key"])
        text = readme_text((await context.http_response.read()).decode("utf-8", "replace"))
        if record is not None and len(text) > 200:
            # The model card's opening is its announcement; the rest is usage docs.
            record["summary"] = "{} {}".format(record["summary"], text[:1500])

    # -- phase 2: trend sources ----------------------------------------------
    @router.handler(HF_PAPERS)
    async def hf_papers(context: BeautifulSoupCrawlingContext) -> None:
        for item in await read_json(context) or []:
            paper = item.get("paper") or {}
            published = paper.get("submittedOnDailyAt") or paper.get("publishedAt")
            if not paper.get("id") or not is_fresh(published, now):
                continue
            submitter = paper.get("submittedOnDailyBy") or {}
            result.put("Hugging Face", "paper:" + paper["id"], {
                "id": paper["id"],
                "kind": "paper",
                "title": paper.get("title", ""),
                "summary": paper.get("summary", ""),
                "url": "https://huggingface.co/papers/" + paper["id"],
                "author": submitter.get("user") or submitter.get("name") or "huggingface.co/papers",
                "created_at": published,
                "likes": paper.get("upvotes", 0),
                "comments": item.get("numComments", 0),
            })

    @router.handler(BSKY_POSTS)
    async def bluesky_posts(context: BeautifulSoupCrawlingContext) -> None:
        data = await read_json(context)
        posts = data.get("posts") or [entry.get("post", {}) for entry in data.get("feed", [])]
        filter_relevance = context.request.user_data.get("filter_relevance", False)
        kept = 0
        for post in posts:
            record = post.get("record") or {}
            text = record.get("text", "")
            if "en" not in (record.get("langs") or ["en"]):
                continue
            if not is_fresh(record.get("createdAt"), now):
                result.stats["stale skipped (BSKY_POSTS)"] += 1
                continue
            if filter_relevance and not is_relevant(text):
                continue
            author = post.get("author") or {}
            if result.put("Bluesky", post.get("uri", ""), {
                "uri": post.get("uri"),
                "url": bluesky_url(post),
                "text": text,
                "created_at": record.get("createdAt"),
                "author": {"handle": author.get("handle"), "created_at": author.get("createdAt")},
                "like_count": post.get("likeCount", 0),
                "repost_count": post.get("repostCount", 0) + post.get("quoteCount", 0),
                "reply_count": post.get("replyCount", 0),
                "query": context.request.user_data.get("query"),
            }):
                kept += 1
            if kept >= settings.MAX_SOCIAL_POSTS_PER_QUERY:
                break

    @router.handler(SE_QUESTIONS)
    async def stackexchange_questions(context: BeautifulSoupCrawlingContext) -> None:
        site = context.request.user_data["site"]
        owners: List[str] = []
        for question in (await read_json(context)).get("items", []):
            if not is_fresh(question.get("creation_date"), now):
                continue
            owner = question.get("owner") or {}
            if result.put("StackExchange", "{}:{}".format(site, question["question_id"]), {
                "question_id": question["question_id"],
                "site": site,
                "link": question.get("link"),
                "title": strip_html(question.get("title", "")),
                "body": strip_html(question.get("body", ""))[: settings.MAX_ARTICLE_CHARS],
                "creation_date": question.get("creation_date"),
                "tags": question.get("tags", []),
                "owner": {"user_id": owner.get("user_id"),
                          "display_name": owner.get("display_name"),
                          "reputation": owner.get("reputation", 0)},
                "score": question.get("score", 0),
                "answer_count": question.get("answer_count", 0),
                "view_count": question.get("view_count", 0),
            }) and owner.get("user_id"):
                owners.append(str(owner["user_id"]))
        if owners:
            await context.add_requests([Request.from_url(
                "https://api.stackexchange.com/2.3/users/{}?site={}&pagesize=100".format(
                    ";".join(sorted(set(owners))[:100]), site),
                label=SE_USERS, user_data={"site": site})])

    @router.handler(SE_USERS)
    async def stackexchange_users(context: BeautifulSoupCrawlingContext) -> None:
        site = context.request.user_data["site"]
        for user in (await read_json(context)).get("items", []):
            result.se_users["{}:{}".format(site, user.get("user_id"))] = user.get("creation_date")

    @router.handler(YT_SEARCH)
    async def youtube_search(context: BeautifulSoupCrawlingContext) -> None:
        ids = [item["id"]["videoId"] for item in (await read_json(context)).get("items", [])
               if item.get("id", {}).get("videoId")]
        if ids:
            await context.add_requests([Request.from_url(
                "https://www.googleapis.com/youtube/v3/videos?part=snippet,statistics&id="
                + ",".join(ids),
                label=YT_VIDEOS, headers={"X-goog-api-key": settings.YOUTUBE_API_KEY})])

    @router.handler(YT_VIDEOS)
    async def youtube_videos(context: BeautifulSoupCrawlingContext) -> None:
        channels: List[str] = []
        for video in (await read_json(context)).get("items", []):
            snippet, stats = video.get("snippet", {}), video.get("statistics", {})
            if not is_relevant(snippet.get("title"), snippet.get("description")):
                continue
            if result.put("YouTube", video["id"], {
                "id": video["id"],
                "url": "https://www.youtube.com/watch?v=" + video["id"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[: settings.MAX_ARTICLE_CHARS],
                "published_at": snippet.get("publishedAt"),
                "channel": {"id": snippet.get("channelId"), "title": snippet.get("channelTitle")},
                "view_count": int(stats.get("viewCount", 0)),
                "like_count": int(stats.get("likeCount", 0)),
                "comment_count": int(stats.get("commentCount", 0)),
            }):
                channels.append(snippet.get("channelId", ""))
        channels = [c for c in dict.fromkeys(channels) if c and c not in result.yt_channels]
        if channels:
            await context.add_requests([Request.from_url(
                "https://www.googleapis.com/youtube/v3/channels?part=snippet,statistics&id="
                + ",".join(channels[:50]),
                label=YT_CHANNELS, headers={"X-goog-api-key": settings.YOUTUBE_API_KEY})])

    @router.handler(YT_CHANNELS)
    async def youtube_channels(context: BeautifulSoupCrawlingContext) -> None:
        for channel in (await read_json(context)).get("items", []):
            result.yt_channels[channel["id"]] = {
                "created_at": channel.get("snippet", {}).get("publishedAt"),
                "subscriber_count": int(channel.get("statistics", {}).get("subscriberCount", 0)),
            }
