"""
ingest.py — Normalisation layer between the existing v1 scrapers and the v2 gates.

v2 AUGMENTS the existing system: nothing here re-architects a scraper. Each
platform adapter simply maps that scraper's native record shape onto the single
intermediate schema (RawSignal) that Gate 1 and Gate 2 consume.

Adding a sixth source later means adding one adapter function and registering it
in PLATFORM_ADAPTERS - no change to any gate.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator

from . import config, text_utils

logger = logging.getLogger("ingest")


# ---------------------------------------------------------------------------
# Intermediate schema (master prompt, module 2)
# ---------------------------------------------------------------------------
class Author(BaseModel):
    author_id: Optional[str] = None
    account_age_days: int = 0
    follower_count: int = 0

    @field_validator("account_age_days", "follower_count", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0


class RawContent(BaseModel):
    title: str = ""
    body: str = ""

    @property
    def combined(self) -> str:
        return "{}\n{}".format(self.title or "", self.body or "").strip()


class EngagementMetrics(BaseModel):
    likes: int = 0
    shares: int = 0
    comments: int = 0

    @field_validator("likes", "shares", "comments", mode="before")
    @classmethod
    def _coerce_int(cls, v: Any) -> int:
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            return 0

    @property
    def total(self) -> int:
        return self.likes + self.shares + self.comments


class RawSignal(BaseModel):
    """One normalised research signal, pre-validation."""

    signal_id: str
    source_platform: str
    source_url: Optional[str] = None
    author: Author = Field(default_factory=Author)
    timestamp: Optional[str] = None  # ISO-8601
    raw_content: RawContent = Field(default_factory=RawContent)
    engagement_metrics: EngagementMetrics = Field(default_factory=EngagementMetrics)

    @field_validator("source_platform")
    @classmethod
    def _known_platform(cls, v: str) -> str:
        if v not in config.SUPPORTED_PLATFORMS:
            raise ValueError(
                "unsupported platform {} (expected one of {})".format(
                    v, config.SUPPORTED_PLATFORMS
                )
            )
        return v

    # -- derived helpers used by both gates --------------------------------
    @property
    def text(self) -> str:
        return self.raw_content.combined

    @property
    def datetime_utc(self) -> Optional[datetime]:
        return parse_timestamp(self.timestamp)

    @property
    def age_hours(self) -> Optional[float]:
        dt = self.datetime_utc
        if dt is None:
            return None
        return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0

    @property
    def domain(self) -> str:
        if not self.source_url:
            return ""
        try:
            host = urlparse(self.source_url).netloc.lower()
        except ValueError:
            return ""
        return host[4:] if host.startswith("www.") else host

    def keywords(self, top_k: int = 12) -> List[str]:
        return text_utils.extract_keywords(self.text, top_k=top_k)


# ---------------------------------------------------------------------------
# Timestamp handling
# ---------------------------------------------------------------------------
def parse_timestamp(value: Any) -> Optional[datetime]:
    """Accept ISO-8601 strings, epoch seconds, or datetimes; always return UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(str(value).strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                logger.debug("Unparseable timestamp: %s", value)
                return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def to_iso(value: Any) -> Optional[str]:
    dt = parse_timestamp(value)
    return dt.isoformat() if dt else None


def make_signal_id(platform: str, native_id: Any, url: Any) -> str:
    prefix = {
        "Reddit": "rdt",
        "X": "x",
        "Instagram": "ig",
        "Facebook": "fb",
        "Google Search": "gsr",
        "Webpage": "web",
        "Hacker News": "hn",
        "Mastodon": "mst",
        "News": "nws",
        "Google Trends": "gtr",
        "Bluesky": "bsky",
        "Hugging Face": "hf",
        "StackExchange": "se",
        "YouTube": "yt",
        "GitHub": "gh",
    }.get(platform, "sig")
    seed = "{}|{}|{}".format(platform, native_id, url)
    return "sig_{}_{}".format(prefix, hashlib.md5(seed.encode("utf-8")).hexdigest()[:10])


def _get(record: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present, non-empty key wins - scrapers are inconsistent about naming."""
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return default


# ---------------------------------------------------------------------------
# Per-platform adapters
# ---------------------------------------------------------------------------
def from_reddit(record: Dict[str, Any]) -> RawSignal:
    author = _get(record, "author", "author_name", default={}) or {}
    if isinstance(author, str):
        author = {"author_id": author}
    url = _get(record, "url", "permalink", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("Reddit", _get(record, "id", "post_id", default=url), url),
        source_platform="Reddit",
        source_url=url,
        author=Author(
            author_id=_get(author, "name", "author_id", "id"),
            account_age_days=_get(author, "account_age_days", "age_days", default=0),
            follower_count=_get(author, "karma", "follower_count", "subscribers", default=0),
        ),
        timestamp=to_iso(_get(record, "created_utc", "created_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "selftext", "body", "text", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "ups", "score", "likes", default=0),
            shares=_get(record, "crossposts", "shares", default=0),
            comments=_get(record, "num_comments", "comments", default=0),
        ),
    )


def from_x(record: Dict[str, Any]) -> RawSignal:
    user = _get(record, "user", "author", default={}) or {}
    if isinstance(user, str):
        user = {"author_id": user}
    url = _get(record, "url", "tweet_url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("X", _get(record, "tweet_id", "id", default=url), url),
        source_platform="X",
        source_url=url,
        author=Author(
            author_id=_get(user, "screen_name", "username", "author_id", "id"),
            account_age_days=_get(user, "account_age_days", "age_days", default=0),
            follower_count=_get(user, "followers_count", "follower_count", default=0),
        ),
        timestamp=to_iso(_get(record, "created_at", "timestamp")),
        raw_content=RawContent(
            title="",
            body=_get(record, "text", "full_text", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "favorite_count", "like_count", "likes", default=0),
            shares=_get(record, "retweet_count", "shares", default=0),
            comments=_get(record, "reply_count", "comments", default=0),
        ),
    )


def from_instagram(record: Dict[str, Any]) -> RawSignal:
    owner = _get(record, "owner", "user", "author", default={}) or {}
    if isinstance(owner, str):
        owner = {"author_id": owner}
    url = _get(record, "permalink", "url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("Instagram", _get(record, "pk", "id", default=url), url),
        source_platform="Instagram",
        source_url=url,
        author=Author(
            author_id=_get(owner, "username", "author_id", "id"),
            account_age_days=_get(owner, "account_age_days", "age_days", default=0),
            follower_count=_get(owner, "follower_count", "followers", default=0),
        ),
        timestamp=to_iso(_get(record, "taken_at", "timestamp", "created_at")),
        raw_content=RawContent(
            title="",
            body=_get(record, "caption", "text", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "like_count", "likes", default=0),
            shares=_get(record, "share_count", "shares", default=0),
            comments=_get(record, "comment_count", "comments", default=0),
        ),
    )


def from_facebook(record: Dict[str, Any]) -> RawSignal:
    sender = _get(record, "from", "author", "page", default={}) or {}
    if isinstance(sender, str):
        sender = {"author_id": sender}
    url = _get(record, "permalink_url", "permalink", "url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("Facebook", _get(record, "post_id", "id", default=url), url),
        source_platform="Facebook",
        source_url=url,
        author=Author(
            author_id=_get(sender, "id", "name", "author_id"),
            account_age_days=_get(sender, "account_age_days", "page_age_days", default=0),
            follower_count=_get(sender, "fan_count", "follower_count", "followers", default=0),
        ),
        timestamp=to_iso(_get(record, "created_time", "timestamp", "created_at")),
        raw_content=RawContent(
            title=_get(record, "name", "title", default="") or "",
            body=_get(record, "message", "description", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "reactions", "like_count", "likes", default=0),
            shares=_get(record, "shares", "share_count", default=0),
            comments=_get(record, "comments", "comment_count", default=0),
        ),
    )


def from_google_search(record: Dict[str, Any]) -> RawSignal:
    url = _get(record, "link", "url", "source_url", default="")
    domain = _get(record, "displayLink", "display_link", default="") or ""
    if not domain and url:
        try:
            domain = urlparse(url).netloc
        except ValueError:
            domain = ""
    return RawSignal(
        signal_id=make_signal_id(
            "Google Search", _get(record, "result_id", "cacheId", default=url), url
        ),
        source_platform="Google Search",
        source_url=url,
        author=Author(
            author_id=domain or None,
            # A publishing domain has no "account age"; treat an indexed domain as
            # established so the age sub-score does not unfairly punish web results.
            account_age_days=_get(
                record, "domain_age_days", default=config.MIN_TRUSTED_ACCOUNT_AGE_DAYS
            ),
            follower_count=_get(
                record, "estimated_monthly_traffic", "follower_count", default=0
            ),
        ),
        timestamp=to_iso(_get(record, "date", "published_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "snippet", "description", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=0,
            shares=_get(record, "backlinks", default=0),
            comments=0,
        ),
    )


def from_webpage(record: Dict[str, Any]) -> RawSignal:
    """Map an explicitly submitted public webpage into the common signal schema."""
    url = _get(record, "url", "source_url", default="")
    domain = _get(record, "domain", default="") or ""
    if not domain and url:
        try:
            domain = urlparse(url).hostname or ""
        except ValueError:
            domain = ""
    return RawSignal(
        signal_id=make_signal_id(
            "Webpage", _get(record, "id", "result_id", default=url), url
        ),
        source_platform="Webpage",
        source_url=url,
        author=Author(
            author_id=_get(record, "author", "author_id", default=domain),
            account_age_days=config.MIN_TRUSTED_ACCOUNT_AGE_DAYS,
        ),
        timestamp=to_iso(_get(record, "published_at", "created_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "body", "text", "description", default="") or "",
        ),
    )


def from_hacker_news(record: Dict[str, Any]) -> RawSignal:
    """Map an HN Algolia story hit (optionally enriched with Firebase user data)."""
    author = _get(record, "author", default={}) or {}
    if isinstance(author, str):
        author = {"author_id": author}
    discussion_url = "https://news.ycombinator.com/item?id={}".format(
        _get(record, "objectID", "id", default="")
    )
    url = _get(record, "url", "source_url", default="") or discussion_url
    return RawSignal(
        signal_id=make_signal_id("Hacker News", _get(record, "objectID", "id", default=url), url),
        source_platform="Hacker News",
        source_url=url,
        author=Author(
            author_id=_get(author, "name", "author_id", "id"),
            account_age_days=_get(author, "account_age_days", default=0),
            follower_count=_get(author, "karma", "follower_count", default=0),
        ),
        timestamp=to_iso(_get(record, "created_at_i", "created_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "article_text", "story_text", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "points", "likes", default=0),
            shares=0,
            comments=_get(record, "num_comments", "comments", default=0),
        ),
    )


def from_github(record: Dict[str, Any]) -> RawSignal:
    """Map a recently active GitHub repository into a research signal."""
    url = _get(record, "url", "html_url", "source_url", default="")
    owner = _get(record, "owner", default={}) or {}
    if isinstance(owner, str):
        owner = {"login": owner}
    topics = _get(record, "topics", default=[]) or []
    return RawSignal(
        signal_id=make_signal_id(
            "GitHub", _get(record, "id", "full_name", default=url), url
        ),
        source_platform="GitHub",
        source_url=url,
        author=Author(author_id=_get(owner, "login", "name")),
        timestamp=to_iso(_get(record, "pushed_at", "updated_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", "full_name", "name", default="") or "",
            body="{} {}".format(
                _get(record, "description", "body", default="") or "",
                " ".join(str(topic) for topic in topics),
            ).strip(),
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "stargazers_count", "stars", default=0),
            shares=_get(record, "forks_count", "forks", default=0),
            comments=_get(record, "open_issues_count", default=0),
        ),
    )


def from_mastodon(record: Dict[str, Any]) -> RawSignal:
    """Map a Mastodon API status. `content` is HTML, so it is reduced to text."""
    account = _get(record, "account", default={}) or {}
    url = _get(record, "url", "uri", "source_url", default="")
    created = parse_timestamp(_get(account, "created_at", default=None))
    age_days = (datetime.now(timezone.utc) - created).days if created else 0
    return RawSignal(
        signal_id=make_signal_id("Mastodon", _get(record, "id", default=url), url),
        source_platform="Mastodon",
        source_url=url,
        author=Author(
            author_id=_get(account, "acct", "username", "id"),
            account_age_days=age_days,
            follower_count=_get(account, "followers_count", default=0),
        ),
        timestamp=to_iso(_get(record, "created_at", "timestamp")),
        raw_content=RawContent(
            title=(_get(record, "card", default={}) or {}).get("title", "") or "",
            body=text_utils.strip_html(_get(record, "content", "text", default="") or ""),
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "favourites_count", "likes", default=0),
            shares=_get(record, "reblogs_count", "shares", default=0),
            comments=_get(record, "replies_count", "comments", default=0),
        ),
    )


def from_news(record: Dict[str, Any]) -> RawSignal:
    """Map a news-search result (feed item, optionally enriched with article text)."""
    url = _get(record, "url", "link", "source_url", default="")
    domain = ""
    if url:
        try:
            domain = urlparse(url).hostname or ""
        except ValueError:
            domain = ""
    return RawSignal(
        signal_id=make_signal_id("News", _get(record, "id", default=url), url),
        source_platform="News",
        source_url=url,
        author=Author(
            author_id=_get(record, "author", "publisher", default=domain) or domain or None,
            # A publisher has no "account age"; an indexed news outlet is established.
            account_age_days=config.MIN_TRUSTED_ACCOUNT_AGE_DAYS,
        ),
        timestamp=to_iso(_get(record, "published_at", "date", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "article_text", "description", "body", default="") or "",
        ),
    )


def _age_days(value: Any) -> int:
    created = parse_timestamp(value)
    return (datetime.now(timezone.utc) - created).days if created else 0


def from_google_trends(record: Dict[str, Any]) -> RawSignal:
    """A trending search term plus the news stories Google attaches to it."""
    news = [n for n in (record.get("news") or []) if isinstance(n, dict)]
    lead = news[0] if news else {}
    url = lead.get("url") or "https://trends.google.com/trending?geo={}".format(
        record.get("geo", ""))
    return RawSignal(
        signal_id=make_signal_id("Google Trends", _get(record, "id", "query", default=url), url),
        source_platform="Google Trends",
        source_url=url,
        author=Author(
            author_id=lead.get("source") or "trends.google.com",
            account_age_days=config.MIN_TRUSTED_ACCOUNT_AGE_DAYS,
        ),
        timestamp=to_iso(_get(record, "published_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "query", "title", default="") or "",
            body=" ".join(n.get("title", "") for n in news if n.get("title")),
        ),
        # Search volume is the engagement Google exposes for a trend.
        engagement_metrics=EngagementMetrics(shares=_get(record, "approx_traffic", default=0)),
    )


def from_bluesky(record: Dict[str, Any]) -> RawSignal:
    author = _get(record, "author", default={}) or {}
    url = _get(record, "url", "uri", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("Bluesky", _get(record, "uri", "id", default=url), url),
        source_platform="Bluesky",
        source_url=url,
        author=Author(
            author_id=_get(author, "handle", "did"),
            account_age_days=_age_days(author.get("created_at")),
            follower_count=_get(author, "followers_count", default=0),
        ),
        timestamp=to_iso(_get(record, "created_at", "timestamp")),
        raw_content=RawContent(body=_get(record, "text", "body", default="") or ""),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "like_count", "likes", default=0),
            shares=_get(record, "repost_count", "shares", default=0),
            comments=_get(record, "reply_count", "comments", default=0),
        ),
    )


def from_hugging_face(record: Dict[str, Any]) -> RawSignal:
    """A trending model or a community-upvoted daily paper."""
    url = _get(record, "url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id(
            "Hugging Face", "{}:{}".format(record.get("kind", ""), _get(record, "id", default=url)), url),
        source_platform="Hugging Face",
        source_url=url,
        author=Author(
            author_id=_get(record, "author", default="huggingface.co"),
            # A publishing org / paper submitter on the Hub is treated like an
            # established publisher: the Hub exposes no account age.
            account_age_days=config.MIN_TRUSTED_ACCOUNT_AGE_DAYS,
        ),
        timestamp=to_iso(_get(record, "created_at", "published_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "summary", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "likes", "upvotes", default=0),
            comments=_get(record, "comments", default=0),
        ),
    )


def from_stackexchange(record: Dict[str, Any]) -> RawSignal:
    owner = _get(record, "owner", default={}) or {}
    url = _get(record, "link", "url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id(
            "StackExchange", "{}:{}".format(record.get("site", ""), record.get("question_id")), url),
        source_platform="StackExchange",
        source_url=url,
        author=Author(
            author_id=_get(owner, "display_name", "user_id"),
            account_age_days=_get(owner, "account_age_days", default=0),
            follower_count=_get(owner, "reputation", default=0),
        ),
        timestamp=to_iso(_get(record, "creation_date", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "score", default=0),
            comments=_get(record, "answer_count", default=0),
        ),
    )


def from_youtube(record: Dict[str, Any]) -> RawSignal:
    channel = _get(record, "channel", default={}) or {}
    url = _get(record, "url", "source_url", default="")
    return RawSignal(
        signal_id=make_signal_id("YouTube", _get(record, "id", default=url), url),
        source_platform="YouTube",
        source_url=url,
        author=Author(
            author_id=_get(channel, "title", "id"),
            account_age_days=_get(channel, "account_age_days", default=0),
            follower_count=_get(channel, "subscriber_count", default=0),
        ),
        timestamp=to_iso(_get(record, "published_at", "timestamp")),
        raw_content=RawContent(
            title=_get(record, "title", default="") or "",
            body=_get(record, "description", "body", default="") or "",
        ),
        engagement_metrics=EngagementMetrics(
            likes=_get(record, "like_count", default=0),
            comments=_get(record, "comment_count", default=0),
        ),
    )


PLATFORM_ADAPTERS: Dict[str, Callable[[Dict[str, Any]], RawSignal]] = {
    "Reddit": from_reddit,
    "X": from_x,
    "Instagram": from_instagram,
    "Facebook": from_facebook,
    "Google Search": from_google_search,
    "Webpage": from_webpage,
    "Hacker News": from_hacker_news,
    "GitHub": from_github,
    "Mastodon": from_mastodon,
    "News": from_news,
    "Google Trends": from_google_trends,
    "Bluesky": from_bluesky,
    "Hugging Face": from_hugging_face,
    "StackExchange": from_stackexchange,
    "YouTube": from_youtube,
}


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def normalize_record(record: Dict[str, Any], platform: str) -> Optional[RawSignal]:
    adapter = PLATFORM_ADAPTERS.get(platform)
    if adapter is None:
        logger.error("No adapter registered for platform %s - record skipped.", platform)
        return None
    try:
        return adapter(record)
    except ValidationError as exc:
        logger.error("Schema validation failed for a %s record: %s", platform, exc.errors()[:2])
    except Exception as exc:  # noqa: BLE001 - one bad record must not kill ingestion
        logger.error("Could not normalise a %s record: %s", platform, exc)
    return None


def ingest(raw_payload: Dict[str, List[Dict[str, Any]]]) -> List[RawSignal]:
    """Normalise a {platform: [raw records]} payload into a flat RawSignal list."""
    signals: List[RawSignal] = []
    for platform, records in (raw_payload or {}).items():
        ok = 0
        for record in records or []:
            signal = normalize_record(record, platform)
            if signal is not None:
                signals.append(signal)
                ok += 1
        logger.info(
            "INGEST | %-14s | %3d/%-3d records normalised", platform, ok, len(records or [])
        )
    logger.info("INGEST | TOTAL          | %d signals entering Gate 1", len(signals))
    return signals
