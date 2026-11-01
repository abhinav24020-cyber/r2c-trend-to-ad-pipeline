"""
config.py — Central configuration for R2C.ai Social Media Automation v2, Week 1.

Holds API credentials, hard rules, thresholds and the Gate 2 composite scoring
weights. Every other module imports from here; no magic numbers live elsewhere.
"""
from __future__ import annotations

import os
from typing import Dict, List


def load_env_file(path: str) -> None:
    """Load KEY=value settings without overwriting variables already in the shell."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except FileNotFoundError:
        return

    for line in lines:
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


PROJECT_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_env_file(os.path.join(PROJECT_DIR, ".env"))

# --------------------------------------------------------------------------
# Gemini API
# --------------------------------------------------------------------------
GEMINI_API_KEY: str = os.environ.get("GEMINI_API_KEY", "")
GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
GEMINI_TEXT_MODEL: str = os.environ.get(
    "GEMINI_TEXT_MODEL", "gemini-3.5-flash-lite"
)

# --------------------------------------------------------------------------
# Advertisement image generation (Week 2)
# --------------------------------------------------------------------------
AD_IMAGE_WIDTH: int = int(os.environ.get("AD_IMAGE_WIDTH", "1024"))
AD_IMAGE_HEIGHT: int = int(os.environ.get("AD_IMAGE_HEIGHT", "1024"))
AD_IMAGE_STEPS: int = int(os.environ.get("AD_IMAGE_STEPS", "30"))

# Images are generated through Cloudflare Workers AI (needs CLOUDFLARE_ACCOUNT_ID
# and CLOUDFLARE_API_TOKEN). It is the only provider; the setting is kept so a
# future provider can be added without changing callers.
IMAGE_PROVIDER: str = os.environ.get("IMAGE_PROVIDER", "cloudflare").strip().lower()
CLOUDFLARE_ACCOUNT_ID: str = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN: str = os.environ.get("CLOUDFLARE_API_TOKEN", "")
# FLUX.2 Klein 4B accepts width/height, so each post's image is made at its
# platform's aspect ratio. FLUX.1 schnell (@cf/black-forest-labs/flux-1-schnell)
# is square-only (1024x1024) and is still supported. Check the model terms
# (bfl.ai/legal) before commercial use.
# Hugging Face Inference Providers (SD 3.5 Medium, Qwen-Image, ...): token with
# "Make calls to Inference Providers" permission, from huggingface.co/settings/tokens.
HF_TOKEN: str = os.environ.get("HF_TOKEN", "").strip()
CLOUDFLARE_IMAGE_MODEL: str = os.environ.get(
    "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-2-klein-4b"
)
# Used only by FLUX.1 schnell, which accepts at most 8 steps.
CLOUDFLARE_IMAGE_STEPS: int = int(os.environ.get("CLOUDFLARE_IMAGE_STEPS", "8"))

# One image per post, at the aspect ratio that suits the post's stage and the
# placement it is meant for. Sizes are ~1 megapixel, multiples of 64.
AD_ASPECT_SIZES: Dict[str, tuple] = {
    "16:9": (1344, 768),    # LinkedIn / X link post, YouTube thumbnail, web banner
    "1.91:1": (1216, 640),  # Facebook feed link ad / Meta Audience Network
    "1:1": (1024, 1024),    # feed post on every platform
    "4:5": (896, 1120),     # Instagram / Facebook portrait feed post
    "3:4": (960, 1280),     # Instagram profile-grid portrait / carousel cover
    "9:16": (768, 1344),    # Stories, Reels, Shorts
}
# How the ad's text gets into the image:
#   "ai"      the image model designs the whole ad (headline, CTA button, R2C.Ai
#             wordmark); Gemini then reads the text back and the image is redone
#             if anything is misspelled, falling back to "overlay" if it stays wrong.
#   "overlay" the model draws a text-free picture and code typesets the text.
# "overlay" (default): text-free picture + R2C brand template typeset by code, always spelled right.
# "ai": the image model draws headline and CTA itself, checked by Gemini (small models misspell often).
AD_TEXT_MODE: str = os.environ.get("R2C_AD_TEXT_MODE", "overlay").strip().lower()
AD_TEXT_RETRIES: int = int(os.environ.get("R2C_AD_TEXT_RETRIES", "2"))

AD_STAGE_ASPECT_RATIO: Dict[str, str] = {
    "awareness": "16:9",   # first touch: wide, scroll-stopping banner
    "interest": "1:1",     # explainer carousel / feed post
    "desire": "4:5",       # portrait feed post that takes more screen
    "action": "9:16",      # full-screen story with a call to action
}

# Text-generation calls are spaced to stay under this rate. The Gemini free tier
# allows 15 requests/minute for flash-lite models; one cycle makes dozens of
# calls (relevance batches, cluster merge, summaries, two scores per topic).
# Set to 0 to disable pacing on a paid tier.
GEMINI_MAX_REQUESTS_PER_MINUTE: float = float(os.environ.get("GEMINI_MAX_REQUESTS_PER_MINUTE", "14"))

# Exponential-backoff policy for every Gemini call.
API_MAX_RETRIES: int = 4
API_INITIAL_BACKOFF_SECONDS: float = 1.0
API_BACKOFF_MULTIPLIER: float = 2.0
API_MAX_BACKOFF_SECONDS: float = 16.0

# When no API key is present (or the SDK is missing) the pipeline degrades to
# deterministic local heuristics instead of crashing, so the Week 1 demo always
# runs end to end. Set to False to make missing credentials a hard error.
ALLOW_OFFLINE_FALLBACK: bool = True

# --------------------------------------------------------------------------
# Platforms (PRD section 7 — the five research sources)
# --------------------------------------------------------------------------
SUPPORTED_PLATFORMS: List[str] = [
    "Reddit", "X", "Instagram", "Facebook", "Google Search", "Webpage",
    # Sources discovered autonomously by web_crawler/ (Crawlee).
    "Hacker News", "Mastodon", "News", "GitHub",
    "Google Trends", "Bluesky", "Hugging Face", "StackExchange", "YouTube",
]

# --------------------------------------------------------------------------
# GATE 1 — Data quality thresholds
# --------------------------------------------------------------------------
MIN_CROSS_PLATFORM_COUNT: int = 2          # hard rule: a trend needs >= 2 platforms
MIN_CREDIBILITY_SCORE: float = 40.0        # drop signals scoring below this (0-100)
FRESHNESS_CUTOFF_HOURS: float = 72.0       # anything older than this is REJECT_STALE
FRESHNESS_DECAY_LAMBDA: float = 0.0128     # e^(-lambda * hours); ~0.40 weight at 72h

# Near-duplicate detection
EXACT_DUPLICATE_HASH: str = "md5"
MINHASH_PERMUTATIONS: int = 64
MINHASH_SHINGLE_SIZE: int = 3
NEAR_DUPLICATE_MINHASH_THRESHOLD: float = 0.70   # Jaccard estimate
NEAR_DUPLICATE_COSINE_THRESHOLD: float = 0.85    # TF-IDF cosine

# Spam / bot heuristics
MAX_HASHTAGS: int = 8
MAX_URLS: int = 3
MAX_CAPS_RATIO: float = 0.55               # shouting / bot syntax
MIN_CONTENT_CHARS: int = 40
MAX_REPEATED_TOKEN_RATIO: float = 0.35     # keyword stuffing
SPAM_KEYWORDS: List[str] = [
    "buy now", "click here", "limited offer", "free trial", "promo code",
    "dm me", "link in bio", "earn money", "get rich", "crypto giveaway",
    "airdrop", "guaranteed returns", "subscribe now", "follow back",
    "100% free", "act fast", "make $", "work from home", "casino", "betting",
]

# Credibility sub-weights (must sum to 1.0)
CREDIBILITY_WEIGHTS: Dict[str, float] = {
    "account_age": 0.30,
    "engagement_ratio": 0.35,
    "domain_authority": 0.35,
}
MIN_TRUSTED_ACCOUNT_AGE_DAYS: int = 365    # age at which the age sub-score saturates
SUSPICIOUS_ENGAGEMENT_RATIO: float = 0.60  # engagement/follower above this = inorganic

# Domain / platform authority baselines (0-100)
PLATFORM_AUTHORITY: Dict[str, float] = {
    "Reddit": 70.0,
    "X": 65.0,
    "Instagram": 55.0,
    "Facebook": 55.0,
    "Google Search": 75.0,
    "Hacker News": 72.0,
    "Mastodon": 55.0,
    "News": 65.0,
    "Google Trends": 80.0,
    "Bluesky": 55.0,
    "Hugging Face": 84.0,
    "GitHub": 82.0,
    "StackExchange": 68.0,
    "YouTube": 60.0,
}
DOMAIN_AUTHORITY: Dict[str, float] = {
    "arxiv.org": 96.0,
    "nature.com": 96.0,
    "science.org": 95.0,
    "openai.com": 88.0,
    "deepmind.google": 88.0,
    "ai.meta.com": 86.0,
    "anthropic.com": 88.0,
    "huggingface.co": 84.0,
    "github.com": 82.0,
    "techcrunch.com": 74.0,
    "theverge.com": 72.0,
    "venturebeat.com": 68.0,
    "medium.com": 45.0,
    "blogspot.com": 30.0,
    "wordpress.com": 30.0,
}
DEFAULT_DOMAIN_AUTHORITY: float = 50.0

# --------------------------------------------------------------------------
# GATE 2 — Trend intelligence
# --------------------------------------------------------------------------
# Composite cutoff for a qualifying trend. Was 70.0 when topics were broad
# keyword groups: a blob of several stories piled up mentions and platforms and
# cleared 70 easily. With one cluster per specific story (gate2_cluster), each
# topic has fewer signals, and on the 25 Oct 2026 crawl 70 let through a single
# story while clearly real ones (Kimi K3.1 Pro, MiniMax M3.1, DeepSeek V4.1)
# scored 61-66. Lowered again to 50 on 25 Oct 2026: with the two-platform rule
# and the brand-fit gate still applied, 50 qualified six real stories on that
# crawl (vs three at 60) while still excluding the off-brand ones. Override with
# R2C_MIN_TREND_SCORE.
MIN_TREND_SCORE_THRESHOLD: float = float(os.environ.get("R2C_MIN_TREND_SCORE", "50"))
# Dropped topics scoring within this many points below the threshold (and not
# off-brand) are flagged "near_miss" in dropped_trends.json, so a reviewer can
# promote a real story that narrowly missed.
NEAR_MISS_MARGIN: float = 5.0
MIN_CLUSTER_SIZE: int = 2                  # a single mention is not a topic

# Only the lead of a signal is embedded for clustering: the title plus the first
# few hundred characters identify the story. Embedding a whole article pulls the
# vector toward the publisher's boilerplate and generic AI vocabulary, which is
# what made unrelated stories from one site cluster together.
CLUSTER_EMBED_CHARS: int = 700

# Clusters are built by average-linkage agglomeration: two clusters merge while
# the MEAN pairwise cosine between their members stays above the threshold, so
# a chain of loosely similar posts cannot pull distinct stories into one blob.
# Two values because the two embedding backends put related text on different
# parts of the cosine range. Calibrated on live crawl data (25 Oct 2026, 91
# validated signals, lead-only Gemini embeddings): same-story pairs scored
# 0.81-0.95, related-but-different stories 0.62-0.70, and the 95th percentile
# of all pairs was 0.68. 0.72 sits in that gap. 0.75 was tried and split
# stories too finely: on the same crawl only 11 topics (vs 18) reached two
# platforms and only 2 trends qualified. gate2_cluster picks the value for the
# active client.
CLUSTER_SIMILARITY_THRESHOLD: float = 0.72          # Gemini embeddings
CLUSTER_SIMILARITY_THRESHOLD_LEXICAL: float = 0.30  # offline lexical fallback

# After agglomeration, cluster pairs whose centroids are at least this similar
# are shown to Gemini, which merges only those that report the same specific
# story (release, paper, incident, deal). Below the floor no merge is considered,
# so a wrong model answer cannot fuse unrelated topics. Raised from 0.60 after
# Gemini merged a different story about the same model into a benchmark story.
CLUSTER_MERGE_CANDIDATE_COSINE: float = 0.65
CLUSTER_MERGE_CANDIDATE_COSINE_LEXICAL: float = 0.20

GROWTH_WINDOW_HOURS: float = 72.0          # window split in half for velocity compare

# Mention velocity counts signals by platform weight, so ten short Bluesky posts
# do not register as the same surge as ten news articles or a trending model.
PLATFORM_SIGNAL_WEIGHT: Dict[str, float] = {
    "Google Trends": 1.5,
    "Hugging Face": 1.5,
    "Hacker News": 1.3,
    "GitHub": 1.2,
    "News": 1.2,
    "Google Search": 1.2,
    "Reddit": 1.0,
    "Webpage": 1.0,
    "StackExchange": 1.0,
    "YouTube": 1.0,
    "X": 0.6,
    "Facebook": 0.6,
    "Bluesky": 0.5,
    "Mastodon": 0.5,
    "Instagram": 0.5,
}
DEFAULT_PLATFORM_SIGNAL_WEIGHT: float = 1.0

# Brand-fit gate: a topic can clear the composite threshold on growth and
# credibility alone while being a poor fit for R2C's audience (r2c_relevance is
# only 10% of the composite). Topics below this R2C-relevance score are dropped
# as DROPPED_OFF_BRAND instead of being handed to content generation.
MIN_R2C_RELEVANCE_SCORE: float = 40.0

# Composite trend score weights (PRD section 7 / master prompt module 7).
WEIGHTS: Dict[str, float] = {
    "w_credibility": 0.20,
    "w_growth_rate": 0.20,
    "w_recency": 0.15,
    "w_cross_platform": 0.20,
    "w_research": 0.15,
    "w_r2c": 0.10,
}

# Cross-platform sub-score ladder (module 7 spec).
CROSS_PLATFORM_SCORE_LADDER: Dict[int, float] = {0: 0.0, 1: 0.0, 2: 80.0, 3: 90.0}
CROSS_PLATFORM_SCORE_MAX: float = 100.0    # 4+ platforms

# R2C business/audience context used when prompting Gemini for relevance scores.
R2C_DOMAIN_CONTEXT: str = (
    "R2C.ai turns research signals into social media content. Its audience is "
    "AI/ML practitioners, technical founders, research engineers and data teams. "
    "Relevant topics: applied AI research, LLM and agent systems, ML tooling and "
    "infrastructure, benchmarks and evaluation, AI product engineering, and "
    "research-to-production workflows."
)
RESEARCH_CRITERIA: str = (
    "Technical/research substance: does the topic concern published research, "
    "benchmarks, methods, datasets, model releases, reproducible engineering "
    "results, or peer-reviewed findings, as opposed to opinion, gossip, "
    "marketing or consumer chatter?"
)

# --------------------------------------------------------------------------
# Storage & output paths
# --------------------------------------------------------------------------
DATA_DIR: str = os.path.join(PROJECT_DIR, "data", "output")
INPUT_DIR: str = os.path.join(PROJECT_DIR, "data", "input")
VALIDATED_SIGNALS_JSON: str = os.path.join(DATA_DIR, "validated_signals.json")
SIGNALS_DB: str = os.path.join(DATA_DIR, "signals.db")
POST_HISTORY_JSON: str = os.path.join(DATA_DIR, "post_history.json")
REJECTED_SIGNALS_JSON: str = os.path.join(DATA_DIR, "rejected_signals.json")
RANKED_TRENDS_JSON: str = os.path.join(DATA_DIR, "ranked_trends.json")
DROPPED_TRENDS_JSON: str = os.path.join(DATA_DIR, "dropped_trends.json")
GENERATED_PROMPTS_JSON: str = os.path.join(DATA_DIR, "generated_prompts.json")
GENERATED_ADS_JSON: str = os.path.join(DATA_DIR, "generated_ad_samples.json")
GENERATED_ADS_DIR: str = os.path.join(DATA_DIR, "generated_images")

STORAGE_BACKEND: str = "both"              # "json" | "sqlite" | "both"

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("R2C_LOG_LEVEL", "INFO")
LOG_FORMAT: str = "%(asctime)s | %(levelname)-7s | %(name)-16s | %(message)s"


def validate_config() -> None:
    """Fail fast on a mis-edited config rather than producing silently wrong scores."""
    total = sum(WEIGHTS.values())
    if abs(total - 1.0) > 1e-6:
        raise ValueError(f"Gate 2 WEIGHTS must sum to 1.0, got {total:.4f}")
    cred_total = sum(CREDIBILITY_WEIGHTS.values())
    if abs(cred_total - 1.0) > 1e-6:
        raise ValueError(f"CREDIBILITY_WEIGHTS must sum to 1.0, got {cred_total:.4f}")
    if MIN_CROSS_PLATFORM_COUNT < 2:
        raise ValueError("PRD hard rule: MIN_CROSS_PLATFORM_COUNT must be >= 2")
