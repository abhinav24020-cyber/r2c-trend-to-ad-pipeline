"""
sample_data.py — Demo fixture standing in for the existing v1 scrapers.

Produces raw records in each platform's NATIVE shape (reddit uses `selftext`/
`ups`, X uses `full_text`/`favorite_count`, and so on) so that ingest.py's
adapters are genuinely exercised rather than bypassed.

Timestamps are generated relative to "now" at call time, so the freshness and
growth-rate logic behaves identically no matter when the demo is run.

The fixture deliberately contains at least one record that must die at each
Gate 1 check: spam, exact duplicate, near-duplicate, low credibility, stale,
unattributed, and single-source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _epoch_ago(hours: float) -> float:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()


def build_sample_raw_data() -> Dict[str, List[Dict[str, Any]]]:
    """Return {platform: [native raw records]} exactly as the v1 scrapers emit."""
    return {
        # ------------------------------------------------------------------
        "Reddit": [
            {   # Topic A — agent benchmark (healthy, recent)
                "id": "t3_ag1",
                "url": "https://www.reddit.com/r/MachineLearning/comments/ag1/",
                "title": "New multi-agent reasoning benchmark shows tool-use gains plateau",
                "selftext": (
                    "The AgentBench-2 release evaluates 14 autonomous agent systems on long-horizon "
                    "planning and tool use. Headline result: tool-calling accuracy improves sharply up "
                    "to about six steps and then plateaus, with error compounding dominating beyond "
                    "that. Full evaluation harness and per-task traces are open sourced."
                ),
                "created_utc": _epoch_ago(9),
                "author": {"name": "ml_researcher_kt", "account_age_days": 1460, "karma": 45000},
                "ups": 870, "num_comments": 143, "crossposts": 12,
            },
            {   # Exact repost of the above -> REJECT_DUPLICATE
                "id": "t3_ag1_repost",
                "url": "https://www.reddit.com/r/artificial/comments/ag1rp/",
                "title": "New multi-agent reasoning benchmark shows tool-use gains plateau",
                "selftext": (
                    "The AgentBench-2 release evaluates 14 autonomous agent systems on long-horizon "
                    "planning and tool use. Headline result: tool-calling accuracy improves sharply up "
                    "to about six steps and then plateaus, with error compounding dominating beyond "
                    "that. Full evaluation harness and per-task traces are open sourced."
                ),
                "created_utc": _epoch_ago(6),
                "author": {"name": "repost_bot_92", "account_age_days": 220, "karma": 3100},
                "ups": 41, "num_comments": 3, "crossposts": 0,
            },
            {   # Topic B — on-device small models
                "id": "t3_sm1",
                "url": "https://www.reddit.com/r/LocalLLaMA/comments/sm1/",
                "title": "Small open-weight models are closing the gap on device inference",
                "selftext": (
                    "Benchmarked four open weight 3B models on a laptop NPU this week. Quantised "
                    "throughput is now good enough for interactive use, and quality on summarisation "
                    "is within a few points of much larger hosted models. Full numbers and the "
                    "quantisation recipe are in the linked repo."
                ),
                "created_utc": _epoch_ago(20),
                "author": {"name": "edge_infer", "account_age_days": 980, "karma": 27000},
                "ups": 612, "num_comments": 208, "crossposts": 5,
            },
            {   # Topic D — single platform only -> REJECT_SINGLE_SOURCE
                "id": "t3_stp1",
                "url": "https://www.reddit.com/r/gradadmissions/comments/stp1/",
                "title": "How are people handling PhD stipend negotiation this cycle",
                "selftext": (
                    "Curious how others approached stipend negotiation after an offer came in below "
                    "the departmental average. Did anyone successfully renegotiate, and what argument "
                    "actually worked with the graduate office?"
                ),
                "created_utc": _epoch_ago(14),
                "author": {"name": "phd_hopeful_24", "account_age_days": 700, "karma": 4200},
                "ups": 180, "num_comments": 96, "crossposts": 0,
            },
            {   # Same single-source topic, still one platform
                "id": "t3_stp2",
                "url": "https://www.reddit.com/r/gradadmissions/comments/stp2/",
                "title": "Stipend negotiation outcome thread for this admissions cycle",
                "selftext": (
                    "Sharing my stipend negotiation outcome for the graduate offer I received. The "
                    "department matched a competing offer after I documented the cost of living gap, "
                    "so negotiation is clearly possible in this cycle."
                ),
                "created_utc": _epoch_ago(11),
                "author": {"name": "negotiated_ok", "account_age_days": 1100, "karma": 8800},
                "ups": 240, "num_comments": 77, "crossposts": 1,
            },
            {   # Stale -> REJECT_STALE
                "id": "t3_old1",
                "url": "https://www.reddit.com/r/MachineLearning/comments/old1/",
                "title": "Retrieval augmented generation evaluation datasets worth using",
                "selftext": (
                    "Collecting the retrieval evaluation datasets that actually correlate with "
                    "downstream answer quality. Most public RAG benchmarks still measure recall "
                    "rather than grounded answer correctness, which is the real failure mode."
                ),
                "created_utc": _epoch_ago(140),
                "author": {"name": "rag_eval_person", "account_age_days": 1600, "karma": 33000},
                "ups": 400, "num_comments": 88, "crossposts": 2,
            },
        ],
        # ------------------------------------------------------------------
        "X": [
            {   # Topic A on X
                "tweet_id": "1901",
                "url": "https://x.com/agentlab/status/1901",
                "full_text": (
                    "AgentBench-2 is out. Fourteen autonomous agent stacks measured on long horizon "
                    "planning, and the interesting finding is that error compounding, not raw tool "
                    "accuracy, is what caps performance past six steps. Harness is open."
                ),
                "created_at": _ago(7),
                "user": {"screen_name": "agentlab", "account_age_days": 2100, "followers_count": 24000},
                "favorite_count": 1420, "retweet_count": 386, "reply_count": 94,
            },
            {   # Near-duplicate reword of the tweet above -> REJECT_DUPLICATE
                "tweet_id": "1902",
                "url": "https://x.com/agent_news_feed/status/1902",
                "full_text": (
                    "AgentBench-2 is out. Fourteen autonomous agent stacks measured on long horizon "
                    "planning, and the interesting finding is that error compounding, not raw tool "
                    "accuracy, caps performance past six steps. The harness is open."
                ),
                "created_at": _ago(5),
                "user": {"screen_name": "agent_news_feed", "account_age_days": 400, "followers_count": 5400},
                "favorite_count": 60, "retweet_count": 14, "reply_count": 2,
            },
            {   # Topic B on X
                "tweet_id": "1903",
                "url": "https://x.com/edge_ml/status/1903",
                "full_text": (
                    "Open weight 3B models running on laptop NPUs now hit interactive latency with "
                    "4-bit quantisation. Summarisation quality sits a few points behind hosted "
                    "frontier models. On device inference is genuinely usable this quarter."
                ),
                "created_at": _ago(16),
                "user": {"screen_name": "edge_ml", "account_age_days": 1800, "followers_count": 51000},
                "favorite_count": 2600, "retweet_count": 540, "reply_count": 175,
            },
            {   # Topic C on X — RAG grounding evaluation (fresh, separate from stale Reddit post)
                "tweet_id": "1904",
                "url": "https://x.com/retrieval_eval/status/1904",
                "full_text": (
                    "Released a grounded answer correctness dataset for retrieval pipelines. Recall "
                    "at k barely predicts whether the generated answer is actually supported by the "
                    "retrieved passages. Evaluation code and annotations are public."
                ),
                "created_at": _ago(22),
                "user": {"screen_name": "retrieval_eval", "account_age_days": 1300, "followers_count": 18000},
                "favorite_count": 940, "retweet_count": 222, "reply_count": 61,
            },
            {   # Spam -> REJECT_SPAM
                "tweet_id": "1905",
                "url": "https://x.com/promo_ai_deals/status/1905",
                "full_text": (
                    "BUY NOW! AI agent course 100% free trial, limited offer, link in bio, dm me for "
                    "a promo code and start to earn money with agents today!"
                ),
                "created_at": _ago(3),
                "user": {"screen_name": "promo_ai_deals", "account_age_days": 60, "followers_count": 9000},
                "favorite_count": 12, "retweet_count": 4, "reply_count": 0,
            },
            {   # Low credibility -> REJECT_LOW_CREDIBILITY
                "tweet_id": "1906",
                "url": "https://x.com/brandnew_acct/status/1906",
                "full_text": (
                    "I think autonomous agent benchmarks are basically meaningless and everybody "
                    "measuring long horizon planning is wasting their time honestly."
                ),
                "created_at": _ago(4),
                "user": {"screen_name": "brandnew_acct", "account_age_days": 19, "followers_count": 80},
                "favorite_count": 0, "retweet_count": 0, "reply_count": 0,
            },
            {   # Missing url + user -> REJECT_UNATTRIBUTED
                "tweet_id": "1907",
                "full_text": (
                    "Multi agent planning benchmark results look strong for the open source stacks "
                    "released this week, worth reading the harness code."
                ),
                "created_at": _ago(8),
                "favorite_count": 300, "retweet_count": 40, "reply_count": 11,
            },
        ],
        # ------------------------------------------------------------------
        "Instagram": [
            {   # Topic C on Instagram
                "pk": "ig_5501",
                "permalink": "https://www.instagram.com/p/ig5501/",
                "caption": (
                    "New open dataset for measuring whether a retrieval pipeline's answer is really "
                    "grounded in its sources. Swipe for the annotation methodology and the "
                    "correlation charts against retrieval recall. #AI #research #retrieval"
                ),
                "taken_at": _ago(26),
                "owner": {"username": "ai.research.daily", "account_age_days": 900, "follower_count": 92000},
                "like_count": 5200, "comment_count": 310, "share_count": 480,
            },
            {   # Hashtag stuffing -> REJECT_SPAM
                "pk": "ig_5502",
                "permalink": "https://www.instagram.com/p/ig5502/",
                "caption": (
                    "Agents are changing everything for creators this year and the tools keep getting "
                    "better every single week. #ai #agents #tech #startup #coding #ml #dev #future "
                    "#innovation #automation #saas #growth"
                ),
                "taken_at": _ago(10),
                "owner": {"username": "growth.hacks.ai", "account_age_days": 300, "follower_count": 40000},
                "like_count": 800, "comment_count": 25, "share_count": 12,
            },
        ],
        # ------------------------------------------------------------------
        "Facebook": [
            {   # Topic A on Facebook
                "post_id": "fb_7001",
                "permalink_url": "https://www.facebook.com/airesearchhub/posts/7001",
                "name": "Autonomous agent benchmark results published",
                "message": (
                    "A new benchmark for autonomous agents evaluated fourteen systems on long horizon "
                    "planning and tool use. Researchers report that compounding errors limit results "
                    "beyond roughly six sequential steps, and the full evaluation harness has been "
                    "released for anyone to reproduce."
                ),
                "created_time": _ago(18),
                "from": {"id": "airesearchhub", "name": "AI Research Hub", "account_age_days": 1500,
                         "fan_count": 130000},
                "reactions": 3100, "shares": 410, "comments": 226,
            },
            {   # Topic B on Facebook
                "post_id": "fb_7002",
                "permalink_url": "https://www.facebook.com/mlengineering/posts/7002",
                "name": "On device inference with small open weight models",
                "message": (
                    "Engineers are reporting that quantised three billion parameter open weight models "
                    "now run at interactive speed on laptop hardware, putting local inference within "
                    "reach for everyday summarisation workloads without a hosted API."
                ),
                "created_time": _ago(30),
                "from": {"id": "mlengineering", "name": "ML Engineering", "account_age_days": 2000,
                         "fan_count": 88000},
                "reactions": 1450, "shares": 190, "comments": 98,
            },
        ],
        # ------------------------------------------------------------------
        "Google Search": [
            {   # Topic A on Google Search (arxiv — high authority)
                "result_id": "gs_9001",
                "link": "https://arxiv.org/abs/2609.01234",
                "displayLink": "arxiv.org",
                "title": "AgentBench-2: Evaluating Long-Horizon Tool Use in Autonomous Agents",
                "snippet": (
                    "We introduce AgentBench-2, an evaluation suite covering fourteen autonomous agent "
                    "systems across long-horizon planning and tool-use tasks, and show that compounding "
                    "error rather than single-step tool accuracy bounds task completion."
                ),
                "date": _ago(28),
                "backlinks": 180,
            },
            {   # Topic B on Google Search
                "result_id": "gs_9002",
                "link": "https://huggingface.co/blog/on-device-small-models",
                "displayLink": "huggingface.co",
                "title": "Small open-weight models for on-device inference: a 2026 benchmark",
                "snippet": (
                    "Quantised three billion parameter open-weight models reach interactive throughput "
                    "on consumer NPUs, with summarisation quality within a few points of hosted "
                    "frontier models. Full benchmark methodology and recipes included."
                ),
                "date": _ago(34),
                "backlinks": 95,
            },
            {   # Low credibility domain -> REJECT_LOW_CREDIBILITY
                "result_id": "gs_9003",
                "link": "https://ai-secrets-revealed.blogspot.com/agents-2026",
                "displayLink": "ai-secrets-revealed.blogspot.com",
                "title": "The truth about autonomous agent benchmarks nobody tells you",
                "snippet": (
                    "Our analysis of autonomous agent benchmark results suggests the published planning "
                    "numbers are not what they appear and the industry is hiding the real figures."
                ),
                "date": _ago(12),
                "domain_age_days": 20,
                "backlinks": 2,
            },
        ],
    }


def sample_record_count() -> int:
    return sum(len(v) for v in build_sample_raw_data().values())
