"""
settings.py — What the autonomous crawler looks for, and how hard it looks.

Every list here can be edited freely; the environment variables at the bottom
override the most common knobs without touching code.
"""
from __future__ import annotations

import os
from typing import Dict, List

from trend_pipeline import config as pipeline_config

# --------------------------------------------------------------------------
# Topic seeds. Each cycle searches every source for every query. The autopilot
# also appends the titles of the previous cycle's ranked trends, so the crawler
# follows emerging stories on its own (see autopilot.next_cycle_queries).
# --------------------------------------------------------------------------
SEED_QUERIES: List[str] = [
    "AI agents",
    "large language model",
    "open source LLM",
    "AI benchmark",
    "machine learning research",
    "AI model release",
    "MLOps",
    "AI regulation",
    "AI agent security",
    "LLM evaluation benchmark",
    "open weight model release",
    "AI inference optimization",
    "multimodal AI research",
    "robotics foundation model",
    "AI research dataset",
    "AI coding agents",
]

# Mastodon hashtag timelines to read (without the leading #).
MASTODON_INSTANCE: str = "https://mastodon.social"
MASTODON_HASHTAGS: List[str] = [
    "ai", "llm", "machinelearning", "artificialintelligence", "genai", "opensource",
]

# Cheap PRE-filter at fetch time: feed items, Mastodon/HN front-page posts and
# link-expansion pages are only fetched if they mention one of these terms
# (whole-word, case-insensitive). It saves requests; it is not the quality gate.
# The real relevance decision is the Gemini pass in relevance.py, which scores
# every crawled record 0-100 on how central AI/ML is to it.
RELEVANCE_KEYWORDS: List[str] = [
    "ai", "a.i.", "artificial intelligence", "machine learning", "deep learning",
    "llm", "llms", "language model", "neural", "transformer", "agent", "agents",
    "agentic", "benchmark", "inference", "fine-tuning", "fine-tune", "rag",
    "embedding", "diffusion", "multimodal", "gpt", "gemini", "claude", "llama",
    "mistral", "openai", "anthropic", "deepmind", "hugging face", "mlops",
    "dataset", "reasoning", "chatbot", "copilot", "generative",
]

# Research / industry feeds read every cycle, independent of the queries.
RSS_FEEDS: Dict[str, str] = {
    "arXiv cs.AI": "https://rss.arxiv.org/rss/cs.AI",
    "arXiv cs.CL": "https://rss.arxiv.org/rss/cs.CL",
    "arXiv cs.LG": "https://rss.arxiv.org/rss/cs.LG",
    "Hugging Face blog": "https://huggingface.co/blog/feed.xml",
    "Google Research blog": "https://research.google/blog/rss/",
    "OpenAI news": "https://openai.com/news/rss.xml",
    "MIT Technology Review AI": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "TechCrunch AI": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge AI": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "VentureBeat AI": "https://venturebeat.com/category/ai/feed/",
    "NVIDIA Deep Learning": "https://blogs.nvidia.com/blog/category/deep-learning/feed/",
    "Microsoft Research": "https://www.microsoft.com/en-us/research/feed/",
    "Berkeley AI Research": "https://bair.berkeley.edu/blog/feed.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Apple Machine Learning Research": "https://machinelearning.apple.com/rss.xml",
}

# --------------------------------------------------------------------------
# Trend radar — phase 1 of every cycle. These sources say what is trending
# RIGHT NOW; AI-relevant trends become search queries for phase 2.
# --------------------------------------------------------------------------
GOOGLE_TRENDS_GEOS: List[str] = [
    g.strip() for g in os.environ.get("CRAWL_TRENDS_GEOS", "US,IN").split(",") if g.strip()
]
MAX_TRENDING_QUERIES: int = int(os.environ.get("CRAWL_MAX_TRENDING_QUERIES", "12"))
HF_TRENDING_MODELS: int = 20
HF_DAILY_PAPERS: int = 30

# Bluesky public AppView (robots.txt explicitly allows crawling the public API).
BLUESKY_SEARCH_API: str = "https://api.bsky.app"
BLUESKY_PUBLIC_API: str = "https://public.api.bsky.app"
BLUESKY_WHATS_HOT: str = (
    "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
)

# StackExchange: (site, tag) pairs; an empty tag reads the whole site's hot list.
STACKEXCHANGE_QUERIES: List[tuple] = [
    ("ai", ""),
    ("datascience", ""),
    ("stackoverflow", "large-language-model"),
    ("stackoverflow", "huggingface-transformers"),
    ("stackoverflow", "openai-api"),
]

# YouTube Data API v3 (only with YOUTUBE_API_KEY). Each search costs 100 quota
# units of the default 10,000/day, so only the first N queries are searched.
YOUTUBE_API_KEY: str = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_MAX_QUERIES: int = int(os.environ.get("CRAWL_YOUTUBE_MAX_QUERIES", "3"))

# GitHub's unauthenticated Search API is rate-limited, so each cycle searches
# only a small number of the highest-priority queries.
GITHUB_MAX_QUERIES: int = int(os.environ.get("CRAWL_GITHUB_MAX_QUERIES", "3"))

# Subreddits read only when REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set.
# Reddit blocks anonymous JSON access, and its public RSS carries no scores or
# account data, so those posts could never pass Gate 1 credibility.
REDDIT_SUBREDDITS: List[str] = [
    "MachineLearning", "LocalLLaMA", "artificial", "OpenAI", "singularity",
]

# --------------------------------------------------------------------------
# Volume and politeness limits
# --------------------------------------------------------------------------
MAX_ITEMS_PER_SOURCE_QUERY: int = int(os.environ.get("CRAWL_MAX_ITEMS_PER_QUERY", "15"))
MAX_ITEMS_PER_FEED: int = int(os.environ.get("CRAWL_MAX_ITEMS_PER_FEED", "20"))
# Short social posts (Mastodon per hashtag, Bluesky per query) are numerous and
# individually weak; capping them keeps them from drowning out other sources.
MAX_SOCIAL_POSTS_PER_QUERY: int = int(os.environ.get("CRAWL_MAX_SOCIAL_POSTS_PER_QUERY", "8"))
MAX_REQUESTS_PER_CYCLE: int = int(os.environ.get("CRAWL_MAX_REQUESTS", "600"))
MAX_CONCURRENCY: int = int(os.environ.get("CRAWL_MAX_CONCURRENCY", "8"))
MAX_REQUESTS_PER_MINUTE: int = int(os.environ.get("CRAWL_MAX_REQUESTS_PER_MINUTE", "120"))
HN_MIN_POINTS: int = int(os.environ.get("CRAWL_HN_MIN_POINTS", "10"))

# Semantic relevance gate (relevance.py): after the crawl, Gemini scores each
# record 0-100 on how central AI/ML is to it; records below the minimum are
# dropped before the pipeline sees them. Passing mentions ("...also uses AI")
# score ~20, substantive AI items 70+. Without Gemini every record is kept.
MIN_AI_RELEVANCE: int = int(os.environ.get("CRAWL_MIN_AI_RELEVANCE", "50"))
RELEVANCE_BATCH_SIZE: int = 40

# Follow outbound links found inside fetched articles to discover pages the
# seed sources never listed ("open-web expansion"). 0 disables it. Candidate
# links are ranked so primary sources (papers, labs, universities, official
# blogs) are followed before secondary coverage.
LINK_EXPANSION_DEPTH: int = int(os.environ.get("CRAWL_LINK_DEPTH", "1"))
MAX_LINKS_PER_ARTICLE: int = int(os.environ.get("CRAWL_MAX_LINKS_PER_ARTICLE", "5"))
PRIMARY_SOURCE_HOST_SUFFIXES: List[str] = [
    ".edu", ".ac.uk", ".ac.in", ".ac.jp", ".edu.au", ".gov", "arxiv.org", "doi.org",
    "openreview.net", "aclanthology.org", "nature.com", "science.org", "github.com",
    "huggingface.co", "paperswithcode.com", "semanticscholar.org",
]
PRIMARY_SOURCE_PATH_HINTS: List[str] = [
    "/paper", "/papers/", "/abs/", "/research", "/blog/", "/news/", "/press", "/release",
    "/report", "/benchmark", "/dataset", "/model", "/technical",
]

# Items older than Gate 1's freshness cutoff are dropped before any article
# fetch, so the crawler does not spend requests on signals Gate 1 would reject.
MAX_ITEM_AGE_HOURS: float = pipeline_config.FRESHNESS_CUTOFF_HOURS

# Keep enough of each article for the creative stage to quote methods and
# findings, not just the introduction. Gate 2 embeds only the lead
# (config.CLUSTER_EMBED_CHARS), so a long body no longer dilutes clustering.
MAX_ARTICLE_CHARS: int = int(os.environ.get("CRAWL_MAX_ARTICLE_CHARS", "8000"))
USER_AGENT: str = (
    "Mozilla/5.0 (compatible; R2C-TrendCrawler/1.0; +https://r2c.ai)"
)

# Credentials (optional)
REDDIT_CLIENT_ID: str = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET: str = os.environ.get("REDDIT_CLIENT_SECRET", "")

# --------------------------------------------------------------------------
# Output locations
# --------------------------------------------------------------------------
CRAWLED_INPUT_JSON: str = os.path.join(pipeline_config.INPUT_DIR, "crawled_signals.json")
CRAWL_ARCHIVE_DIR: str = os.path.join(pipeline_config.INPUT_DIR, "crawls")
AUTOPILOT_LOG_JSONL: str = os.path.join(pipeline_config.DATA_DIR, "autopilot_runs.jsonl")
