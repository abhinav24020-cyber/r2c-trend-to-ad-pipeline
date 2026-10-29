"""
crawler.py — Autonomous multi-source discovery crawl built on Crawlee.

One crawl cycle:

    trend radar  ──► what is trending right now (Google Trends, Bluesky,
                     Mastodon, Hugging Face — see trending.py); AI-relevant
                     trends become queries
    queries      ──► discovery sources (HN, GitHub, Bing News RSS, Mastodon,
                     Bluesky, Hugging Face papers, StackExchange, YouTube with a key,
                     research RSS feeds, Reddit when credentialed)
                 ──► article pages (Crawlee fetches the real page for full text,
                     publish time and author)
                 ──► open-web expansion (outbound links inside those articles,
                     kept only if fresh and on-topic)
                 ──► {platform: [native records]}  — the exact payload shape
                     trend_pipeline.ingest already consumes

Every request goes through one Crawlee BeautifulSoupCrawler, so robots.txt,
concurrency, rate limiting, retries and URL de-duplication are handled in one
place. Reddit is the exception: it is an authenticated API, not a crawl, and is
fetched with a plain OAuth client before the crawl starts.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse
from urllib.request import Request as UrlRequest, urlopen

import feedparser
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
from crawlee.storage_clients import MemoryStorageClient

from trend_pipeline import config as pipeline_config
from trend_pipeline.ingest import parse_timestamp
from trend_pipeline.url_scraper import validate_public_url

from . import relevance, settings, trending

logger = logging.getLogger("web_crawler")

# Request labels (Crawlee router keys)
HN_SEARCH = "HN_SEARCH"
HN_USER = "HN_USER"
BING_NEWS = "BING_NEWS"
MASTODON_TAG = "MASTODON_TAG"
GITHUB_SEARCH = "GITHUB_SEARCH"
FEED = "FEED"
ARTICLE = "ARTICLE"
DISCOVERED = "DISCOVERED"

# Hosts that are platforms in their own right, login-walled, or never articles.
SKIP_EXPANSION_HOSTS = (
    "twitter.com", "x.com", "t.co", "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "google.com", "bing.com",
    "apple.com", "play.google.com", "amazon.com", "news.ycombinator.com",
    "wikipedia.org", "archive.org", "doubleclick.net",
    # Aggregators and link shorteners: following them finds more aggregators,
    # not primary sources.
    "techmeme.com", "flipboard.com", "feedly.com", "news.google.com", "msn.com",
    "yahoo.com", "linktr.ee", "bit.ly", "t.me", "discord.gg", "patreon.com",
    "bsky.app", "mastodon.social", "threads.net",
)
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".mp4", ".mp3",
    ".zip", ".gz", ".tar", ".exe", ".dmg", ".xml", ".rss", ".json", ".csv",
)
STRIP_TAGS = (
    "script", "style", "noscript", "nav", "footer", "header", "aside", "form",
    "svg", "iframe", "button", "template",
)
_JSONLD_DATE_RE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_RELEVANCE_RE = re.compile(
    r"(?<![a-z0-9])(?:{})(?![a-z0-9])".format(
        "|".join(re.escape(k) for k in sorted(settings.RELEVANCE_KEYWORDS, key=len, reverse=True))
    ),
    re.IGNORECASE,
)


def is_relevant(*texts: Optional[str]) -> bool:
    return bool(_RELEVANCE_RE.search(" ".join(t for t in texts if t)))


def to_utc(value: Any) -> Optional[datetime]:
    """ISO-8601 / epoch (pipeline parser) plus the RFC-822 dates RSS feeds use."""
    dt = parse_timestamp(value)
    if dt is None and isinstance(value, str) and value.strip():
        try:
            dt = parsedate_to_datetime(value.strip())
        except (TypeError, ValueError):
            return None
        dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt


def to_iso(value: Any) -> Optional[str]:
    dt = to_utc(value)
    return dt.isoformat() if dt else None


def is_fresh(value: Any, now: Optional[datetime] = None) -> bool:
    dt = to_utc(value)
    if dt is None:
        return False
    now = now or datetime.now(timezone.utc)
    return now - dt <= timedelta(hours=settings.MAX_ITEM_AGE_HOURS)


def query_to_hashtag(query: str) -> str:
    return re.sub(r"[^a-z0-9]", "", query.lower())


def decode_bing_link(link: str) -> str:
    """Bing News RSS wraps each article in an apiclick redirect; recover the target."""
    parsed = urlparse(link)
    if parsed.netloc.endswith("bing.com") and parsed.path.endswith("apiclick.aspx"):
        target = parse_qs(parsed.query).get("url")
        if target:
            return target[0]
    return link


def _host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def expandable_link(url: str, parent_url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = _host(url)
    if not host or host == _host(parent_url):
        return False  # same-site links are navigation, not new sources
    if any(host == h or host.endswith("." + h) for h in SKIP_EXPANSION_HOSTS):
        return False
    path = parsed.path.lower()
    if path in ("", "/") or path.endswith(SKIP_EXTENSIONS):
        return False
    return True


def expansion_priority(url: str) -> float:
    """
    Rank an outbound link for open-web expansion: primary sources first.

    Domain authority (the same table Gate 1 uses) plus a bonus for research,
    university, government and official-blog hosts and paths, so an article's
    link to the paper or the lab's announcement is followed before its link to
    another news site's coverage.
    """
    host = _host(url)
    path = urlparse(url).path.lower()
    score = pipeline_config.DEFAULT_DOMAIN_AUTHORITY
    for known, authority in pipeline_config.DOMAIN_AUTHORITY.items():
        if host == known or host.endswith("." + known):
            score = authority
            break
    if any(host == s.lstrip(".") or host.endswith(s) for s in settings.PRIMARY_SOURCE_HOST_SUFFIXES):
        score += 25.0
    if any(hint in path for hint in settings.PRIMARY_SOURCE_PATH_HINTS):
        score += 10.0
    return score


MIN_PARAGRAPH_CHARS = 60
MAX_PARAGRAPH_LINK_RATIO = 0.5


def article_paragraphs(container: Any) -> str:
    """
    Join the page's prose paragraphs, skipping link lists.

    "Most popular" / "related stories" blocks are made of short, link-only
    items. They repeat on every page of a site, so when they are kept, unrelated
    articles from the same publisher embed as near-identical and Gate 2 merges
    them into one meaningless cluster.
    """
    kept: List[str] = []
    for paragraph in container.find_all("p"):
        text = " ".join(paragraph.get_text(" ", strip=True).split())
        if len(text) < MIN_PARAGRAPH_CHARS:
            continue
        link_chars = sum(len(a.get_text(strip=True)) for a in paragraph.find_all("a"))
        if link_chars / len(text) > MAX_PARAGRAPH_LINK_RATIO:
            continue
        kept.append(text)
    joined = " ".join(kept)
    return joined if len(joined) >= 200 else ""


def extract_article(context: BeautifulSoupCrawlingContext) -> Dict[str, str]:
    """Pull title, readable text, publish time and author from an HTML page."""
    soup = context.soup
    meta: Dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
        content = (tag.get("content") or "").strip()
        if key and content:
            meta.setdefault(key, content)

    published = (
        meta.get("article:published_time")
        or meta.get("og:published_time")
        or meta.get("datepublished")
        or meta.get("pubdate")
        or meta.get("date")
        or meta.get("dc.date")
    )
    if not published:
        match = _JSONLD_DATE_RE.search(str(soup)[:200000])
        if match:
            published = match.group(1)
    if not published:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            published = time_tag["datetime"]

    title = (
        meta.get("og:title")
        or meta.get("twitter:title")
        or (soup.title.get_text(" ", strip=True) if soup.title else "")
    )
    description = meta.get("og:description") or meta.get("description") or ""

    for tag in soup.find_all(STRIP_TAGS):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = article_paragraphs(container) or " ".join(container.get_text(" ", strip=True).split())
    if description and description not in text:
        text = "{} {}".format(description, text)

    return {
        "title": title[:500],
        "text": text[: settings.MAX_ARTICLE_CHARS],
        "published_at": published or "",
        "author": meta.get("author") or meta.get("article:author") or "",
        "description": description,
        "container": container,
    }


async def _public(url: str) -> bool:
    """SSRF guard shared with the manual URL scraper (DNS lookup off the event loop)."""
    try:
        await asyncio.to_thread(validate_public_url, url)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Collector — accumulates native records during the crawl
# ---------------------------------------------------------------------------
@dataclass
class CrawlResult:
    records: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    hn_users: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    se_users: Dict[str, Any] = field(default_factory=dict)       # "site:user_id" -> creation epoch
    yt_channels: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trending_queries: List[tuple] = field(default_factory=list)  # (query, origin)
    queries: List[str] = field(default_factory=list)             # what phase 2 searched
    off_topic: List[Dict[str, Any]] = field(default_factory=list)  # dropped by the relevance gate
    stats: Counter = field(default_factory=Counter)

    def put(self, platform: str, key: str, record: Dict[str, Any]) -> bool:
        bucket = self.records.setdefault(platform, {})
        if key in bucket:
            return False
        bucket[key] = record
        self.stats["{} records".format(platform)] += 1
        return True

    def get(self, platform: str, key: str) -> Optional[Dict[str, Any]]:
        return self.records.get(platform, {}).get(key)

    def payload(self) -> Dict[str, List[Dict[str, Any]]]:
        """Merge author look-ups into their records and return the ingest payload."""
        for record in self.records.get("Hacker News", {}).values():
            name = record.get("author")
            if isinstance(name, str):
                user = self.hn_users.get(name, {})
                record["author"] = {
                    "name": name,
                    "account_age_days": user.get("account_age_days", 0),
                    "karma": user.get("karma", 0),
                }
        now = datetime.now(timezone.utc)

        def age_days(value: Any) -> int:
            created = to_utc(value)
            return (now - created).days if created else 0

        for record in self.records.get("StackExchange", {}).values():
            owner = record.get("owner") or {}
            created = self.se_users.get("{}:{}".format(record.get("site"), owner.get("user_id")))
            owner["account_age_days"] = age_days(created)
        for record in self.records.get("YouTube", {}).values():
            channel = record.get("channel") or {}
            info = self.yt_channels.get(channel.get("id"), {})
            channel["subscriber_count"] = info.get("subscriber_count", 0)
            channel["account_age_days"] = age_days(info.get("created_at"))
        return {
            platform: list(bucket.values())
            for platform, bucket in self.records.items()
            if bucket
        }


# ---------------------------------------------------------------------------
# Seed requests
# ---------------------------------------------------------------------------
def build_seed_requests(queries: Iterable[str]) -> List[Request]:
    since = int((datetime.now(timezone.utc) - timedelta(hours=settings.MAX_ITEM_AGE_HOURS)).timestamp())
    github_since = (datetime.now(timezone.utc)
                    - timedelta(hours=settings.MAX_ITEM_AGE_HOURS)).date().isoformat()
    queries = list(queries)
    seeds: List[Request] = []
    tags = list(dict.fromkeys(settings.MASTODON_HASHTAGS))

    seeds.append(Request.from_url(
        "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=30",
        label=HN_SEARCH, user_data={"filter_relevance": True},
    ))
    for query in queries:
        seeds.append(Request.from_url(
            "https://hn.algolia.com/api/v1/search?query={}&tags=story"
            "&numericFilters=created_at_i>{},points>={}&hitsPerPage={}".format(
                quote_plus(query), since, settings.HN_MIN_POINTS,
                settings.MAX_ITEMS_PER_SOURCE_QUERY),
            label=HN_SEARCH, user_data={"query": query},
        ))
        seeds.append(Request.from_url(
            "https://www.bing.com/news/search?q={}&format=rss&setlang=en-us&cc=us".format(
                quote_plus(query)),
            label=BING_NEWS, user_data={"query": query},
        ))
        tag = query_to_hashtag(query)
        if tag and tag not in tags:
            tags.append(tag)

    for query in queries[: settings.GITHUB_MAX_QUERIES]:
        github_query = "{} pushed:>{}".format(query, github_since)
        seeds.append(Request.from_url(
            "https://api.github.com/search/repositories?q={}&sort=updated&order=desc"
            "&per_page=10".format(quote_plus(github_query)),
            label=GITHUB_SEARCH,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": settings.USER_AGENT,
            },
            user_data={"query": query},
        ))

    seeds.append(Request.from_url(
        "{}/api/v1/trends/statuses?limit=40".format(settings.MASTODON_INSTANCE.rstrip("/")),
        label=MASTODON_TAG, user_data={"tag": "trending"},
    ))
    for tag in tags:
        seeds.append(Request.from_url(
            "{}/api/v1/timelines/tag/{}?limit=40".format(
                settings.MASTODON_INSTANCE.rstrip("/"), quote(tag)),
            label=MASTODON_TAG, user_data={"tag": tag},
        ))
    for name, url in settings.RSS_FEEDS.items():
        seeds.append(Request.from_url(url, label=FEED, user_data={"feed": name}))
    return seeds


# ---------------------------------------------------------------------------
# Reddit (authenticated API, fetched outside the crawl)
# ---------------------------------------------------------------------------
def _reddit_get(url: str, token: str) -> Dict[str, Any]:
    request = UrlRequest(url, headers={
        "Authorization": "bearer " + token, "User-Agent": settings.USER_AGENT,
    })
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def fetch_reddit(result: CrawlResult) -> None:
    if not (settings.REDDIT_CLIENT_ID and settings.REDDIT_CLIENT_SECRET):
        logger.info("CRAWL | Reddit skipped: set REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET to enable it")
        return
    try:
        basic = base64.b64encode("{}:{}".format(
            settings.REDDIT_CLIENT_ID, settings.REDDIT_CLIENT_SECRET).encode()).decode()
        token_request = UrlRequest(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": "Basic " + basic, "User-Agent": settings.USER_AGENT},
        )
        with urlopen(token_request, timeout=20) as response:
            token = json.load(response)["access_token"]
    except Exception as exc:  # noqa: BLE001 - one source failing must not stop the cycle
        logger.error("CRAWL | Reddit auth failed: %s", exc)
        return

    users: Dict[str, Dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    for sub in settings.REDDIT_SUBREDDITS:
        try:
            listing = _reddit_get(
                "https://oauth.reddit.com/r/{}/top?t=day&limit={}".format(
                    sub, settings.MAX_ITEMS_PER_SOURCE_QUERY), token)
        except Exception as exc:  # noqa: BLE001
            logger.error("CRAWL | Reddit r/%s failed: %s", sub, exc)
            continue
        for child in listing.get("data", {}).get("children", []):
            post = child.get("data", {})
            name = post.get("author")
            if not name or name == "[deleted]" or post.get("stickied"):
                continue
            if name not in users:
                try:
                    about = _reddit_get("https://oauth.reddit.com/user/{}/about".format(name), token)
                    data = about.get("data", {})
                    created = parse_timestamp(data.get("created_utc"))
                    users[name] = {
                        "name": name,
                        "account_age_days": (now - created).days if created else 0,
                        "karma": data.get("total_karma", 0),
                    }
                except Exception:  # noqa: BLE001 - suspended/blocked accounts
                    users[name] = {"name": name}
            result.put("Reddit", post.get("id", ""), {
                "id": post.get("id"),
                "url": "https://www.reddit.com" + post.get("permalink", ""),
                "title": post.get("title", ""),
                "selftext": post.get("selftext", ""),
                "created_utc": post.get("created_utc"),
                "author": users[name],
                "ups": post.get("ups", 0),
                "num_comments": post.get("num_comments", 0),
                "crossposts": post.get("num_crossposts", 0),
            })


# ---------------------------------------------------------------------------
# The crawl
# ---------------------------------------------------------------------------
def _build_crawler(result: CrawlResult, max_requests: int) -> BeautifulSoupCrawler:
    crawler = BeautifulSoupCrawler(
        storage_client=MemoryStorageClient(),
        max_requests_per_crawl=max_requests,
        max_request_retries=1,
        respect_robots_txt_file=True,
        concurrency_settings=ConcurrencySettings(
            max_concurrency=settings.MAX_CONCURRENCY,
            desired_concurrency=min(4, settings.MAX_CONCURRENCY),
            max_tasks_per_minute=settings.MAX_REQUESTS_PER_MINUTE,
        ),
        configure_logging=False,
    )
    now = datetime.now(timezone.utc)
    trending.register(crawler, result, now, is_relevant=is_relevant,
                      is_fresh=is_fresh, to_iso=to_iso)

    async def enqueue_article(context, platform: str, key: str, url: str) -> None:
        if url and await _public(url):
            await context.add_requests([Request.from_url(
                url, label=ARTICLE,
                headers={"User-Agent": settings.USER_AGENT},
                user_data={"platform": platform, "key": key, "depth": 0},
            )])

    async def expand_links(context, container, depth: int) -> None:
        if depth >= settings.LINK_EXPANSION_DEPTH:
            return
        base = context.request.loaded_url or context.request.url
        candidates: List[str] = []
        for anchor in container.find_all("a", href=True):
            url = urljoin(base, anchor["href"]).split("#", 1)[0]
            if url not in candidates and expandable_link(url, base):
                candidates.append(url)
        # Primary sources (papers, labs, official blogs) before secondary coverage.
        found = sorted(candidates, key=lambda u: (-expansion_priority(u), candidates.index(u)))
        found = found[: settings.MAX_LINKS_PER_ARTICLE]
        requests = [
            Request.from_url(url, label=DISCOVERED,
                             headers={"User-Agent": settings.USER_AGENT},
                             user_data={"depth": depth + 1, "via": base})
            for url in found if await _public(url)
        ]
        if requests:
            result.stats["links expanded"] += len(requests)
            await context.add_requests(requests)

    @crawler.router.handler(HN_SEARCH)
    async def hn_search(context: BeautifulSoupCrawlingContext) -> None:
        data = json.loads(await context.http_response.read())
        filter_relevance = context.request.user_data.get("filter_relevance", False)
        for hit in data.get("hits", []):
            if not hit.get("title") or not hit.get("author"):
                continue
            if not is_fresh(hit.get("created_at_i"), now):
                result.stats["stale skipped ({})".format(context.request.label)] += 1
                continue
            if filter_relevance and not is_relevant(hit.get("title"), hit.get("story_text")):
                continue
            key = str(hit["objectID"])
            record = {k: hit.get(k) for k in (
                "objectID", "title", "url", "author", "points", "num_comments",
                "created_at_i", "story_text")}
            if result.put("Hacker News", key, record):
                if hit["author"] not in result.hn_users:
                    result.hn_users[hit["author"]] = {}
                    await context.add_requests([Request.from_url(
                        "https://hacker-news.firebaseio.com/v0/user/{}.json".format(
                            quote(hit["author"])),
                        label=HN_USER, user_data={"name": hit["author"]},
                    )])
                await enqueue_article(context, "Hacker News", key, hit.get("url") or "")

    @crawler.router.handler(HN_USER)
    async def hn_user(context: BeautifulSoupCrawlingContext) -> None:
        data = json.loads(await context.http_response.read()) or {}
        created = parse_timestamp(data.get("created"))
        result.hn_users[context.request.user_data["name"]] = {
            "account_age_days": (now - created).days if created else 0,
            "karma": data.get("karma", 0),
        }

    @crawler.router.handler(BING_NEWS)
    async def bing_news(context: BeautifulSoupCrawlingContext) -> None:
        feed = feedparser.parse(await context.http_response.read())
        for entry in feed.entries[: settings.MAX_ITEMS_PER_SOURCE_QUERY]:
            url = decode_bing_link(entry.get("link", ""))
            published = entry.get("published")
            if not url or not is_fresh(published, now):
                result.stats["stale skipped ({})".format(context.request.label)] += 1
                continue
            record = {
                "url": url,
                "title": entry.get("title", ""),
                "description": entry.get("summary", ""),
                "publisher": entry.get("news_source", ""),
                "published_at": to_iso(published),
                "query": context.request.user_data.get("query"),
            }
            if result.put("News", url, record):
                await enqueue_article(context, "News", url, url)

    @crawler.router.handler(GITHUB_SEARCH)
    async def github_search(context: BeautifulSoupCrawlingContext) -> None:
        data = json.loads(await context.http_response.read())
        for repository in data.get("items", []):
            pushed_at = repository.get("pushed_at")
            description = repository.get("description") or ""
            topics = repository.get("topics") or []
            if not is_fresh(pushed_at, now):
                continue
            if not is_relevant(repository.get("full_name"), description, " ".join(topics)):
                continue
            full_name = repository.get("full_name")
            url = repository.get("html_url")
            if not full_name or not url:
                continue
            result.put("GitHub", full_name, {
                "id": repository.get("id"),
                "full_name": full_name,
                "url": url,
                "title": full_name,
                "description": description,
                "topics": topics,
                "created_at": repository.get("created_at"),
                "pushed_at": pushed_at,
                "stargazers_count": repository.get("stargazers_count", 0),
                "forks_count": repository.get("forks_count", 0),
                "open_issues_count": repository.get("open_issues_count", 0),
                "owner": (repository.get("owner") or {}).get("login", ""),
                "query": context.request.user_data.get("query"),
            })

    @crawler.router.handler(MASTODON_TAG)
    async def mastodon_tag(context: BeautifulSoupCrawlingContext) -> None:
        statuses = json.loads(await context.http_response.read())
        kept = 0
        for status in statuses if isinstance(statuses, list) else []:
            if status.get("reblog") or status.get("language") not in (None, "en"):
                continue
            if not is_fresh(status.get("created_at"), now):
                result.stats["stale skipped ({})".format(context.request.label)] += 1
                continue
            card = status.get("card") or {}
            if not is_relevant(status.get("content"), card.get("title")):
                continue
            account = status.get("account") or {}
            record = {
                "id": status.get("id"),
                "url": status.get("url") or status.get("uri"),
                "created_at": status.get("created_at"),
                "content": status.get("content", ""),
                "language": status.get("language"),
                "favourites_count": status.get("favourites_count", 0),
                "reblogs_count": status.get("reblogs_count", 0),
                "replies_count": status.get("replies_count", 0),
                "card": {"title": card.get("title", ""), "url": card.get("url", "")},
                "account": {
                    "acct": account.get("acct"),
                    "created_at": account.get("created_at"),
                    "followers_count": account.get("followers_count", 0),
                },
            }
            if result.put("Mastodon", str(status.get("id")), record):
                kept += 1
            if kept >= settings.MAX_SOCIAL_POSTS_PER_QUERY:
                break

    @crawler.router.handler(FEED)
    async def rss_feed(context: BeautifulSoupCrawlingContext) -> None:
        feed = feedparser.parse(await context.http_response.read())
        name = context.request.user_data.get("feed")
        kept = 0
        for entry in feed.entries:
            url = entry.get("link", "")
            published = entry.get("published") or entry.get("updated")
            if not url or not is_fresh(published, now):
                result.stats["stale skipped ({})".format(context.request.label)] += 1
                continue
            summary = " ".join(
                re.sub(r"<[^>]+>", " ", entry.get("summary", "")).split())
            if not is_relevant(entry.get("title"), summary):
                continue
            record = {
                "url": url,
                "title": entry.get("title", ""),
                "body": summary,
                "author": entry.get("author") or name,
                "published_at": to_iso(published),
                "domain": _host(url),
                "feed": name,
            }
            if result.put("Webpage", url, record):
                kept += 1
                await enqueue_article(context, "Webpage", url, url)
            if kept >= settings.MAX_ITEMS_PER_FEED:
                break

    @crawler.router.handler(ARTICLE)
    async def article(context: BeautifulSoupCrawlingContext) -> None:
        data = context.request.user_data
        record = result.get(data["platform"], data["key"])
        if record is None:
            return
        page = extract_article(context)
        if len(page["text"]) < 200:
            # Paywall / JS-only page: the feed snippet stays, flagged so the
            # record is known to be a teaser rather than the article.
            record["article_fetched"] = False
            result.stats["articles not extracted (paywall/js)"] += 1
            return
        record["article_fetched"] = True
        result.stats["articles enriched"] += 1
        if data["platform"] == "Webpage":
            record["body"] = page["text"]
        else:
            record["article_text"] = page["text"]
        if data["platform"] == "News" and page["author"]:
            record["author"] = page["author"]
        await expand_links(context, page["container"], int(data.get("depth", 0)))

    @crawler.router.handler(DISCOVERED)
    async def discovered(context: BeautifulSoupCrawlingContext) -> None:
        page = extract_article(context)
        url = context.request.loaded_url or context.request.url
        if len(page["text"]) < 300 or not is_fresh(page["published_at"], now):
            return
        if not is_relevant(page["title"], page["text"][:2000]):
            return
        result.stats["pages discovered"] += 1
        result.put("Webpage", url, {
            "url": url,
            "title": page["title"],
            "body": page["text"],
            "author": page["author"] or _host(url),
            "published_at": to_iso(page["published_at"]),
            "domain": _host(url),
            "discovered_via": context.request.user_data.get("via"),
        })
        await expand_links(context, page["container"], int(context.request.user_data.get("depth", 1)))

    @crawler.failed_request_handler
    async def failed(context, error: Exception) -> None:
        result.stats["failed requests"] += 1
        logger.debug("CRAWL | failed %s: %s", context.request.url, error)

    return crawler


# Which trend origins win when there are more trending queries than slots:
# broad public trends first (rarest, strongest signal), then model releases.
TREND_ORIGIN_PRIORITY = ("Google Trends", "Bluesky trending", "Mastodon trending",
                         "Hugging Face trending")
RADAR_MAX_REQUESTS = 60


def pick_trending_queries(found: List[tuple], limit: int) -> List[str]:
    def rank(item: tuple) -> int:
        origin = item[1]
        return next((i for i, p in enumerate(TREND_ORIGIN_PRIORITY) if origin.startswith(p)),
                    len(TREND_ORIGIN_PRIORITY))
    return [query for query, _ in sorted(found, key=rank)[:limit]]


def apply_relevance_gate(result: CrawlResult, minimum: Optional[int] = None) -> None:
    """Score every record with Gemini and drop the off-topic ones (see relevance.py)."""
    payload = {platform: list(bucket.values()) for platform, bucket in result.records.items()}
    kept, dropped, stats = relevance.filter_payload(payload, minimum=minimum)
    result.stats.update(stats)
    result.off_topic = dropped
    if not dropped:
        return
    dropped_ids = {id(item["record"]) for item in dropped}
    for platform, bucket in list(result.records.items()):
        result.records[platform] = {
            key: record for key, record in bucket.items() if id(record) not in dropped_ids
        }
        result.stats["{} records".format(platform)] = len(result.records[platform])


async def crawl_async(
    queries: Optional[Iterable[str]] = None,
    use_radar: bool = True,
    use_relevance_gate: bool = True,
) -> CrawlResult:
    result = CrawlResult()
    base = [q.strip() for q in (queries or settings.SEED_QUERIES) if q.strip()]

    # Phase 1 — trend radar: what is trending right now?
    hot: List[str] = []
    if use_radar:
        radar = _build_crawler(result, RADAR_MAX_REQUESTS)
        await radar.run(trending.radar_seed_requests())
        hot = pick_trending_queries(result.trending_queries, settings.MAX_TRENDING_QUERIES)
        logger.info("CRAWL | trend radar found %d AI-relevant trend(s); using: %s",
                    len(result.trending_queries), ", ".join(hot) or "(none)")
    result.stats["trending queries"] = len(hot)

    # Phase 2 — deep crawl on trending + seed queries.
    all_queries = list(dict.fromkeys(hot + base))
    result.queries = all_queries
    logger.info("CRAWL | %d queries: %s", len(all_queries), ", ".join(all_queries))
    await asyncio.to_thread(fetch_reddit, result)
    crawler = _build_crawler(result, settings.MAX_REQUESTS_PER_CYCLE)
    stats = await crawler.run(
        trending.deep_seed_requests(all_queries) + build_seed_requests(all_queries))
    result.stats["requests finished"] = stats.requests_finished
    result.stats["requests failed"] = stats.requests_failed

    # Phase 3 — semantic relevance gate on everything collected.
    if use_relevance_gate:
        await asyncio.to_thread(apply_relevance_gate, result)
    return result


def crawl(
    queries: Optional[Iterable[str]] = None,
    use_radar: bool = True,
    use_relevance_gate: bool = True,
) -> CrawlResult:
    """Run one discovery cycle (trend radar + deep crawl + relevance gate) and return its records."""
    return asyncio.run(crawl_async(queries, use_radar=use_radar, use_relevance_gate=use_relevance_gate))
