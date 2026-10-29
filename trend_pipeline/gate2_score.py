"""
gate2_score.py — GATE 2 (step 2): composite trend scoring.

Composite Score = w1*S_cred + w2*S_growth + w3*S_recency
                + w4*S_cross_platform + w5*S_research + w6*S_r2c

All six sub-scores are on a 0-100 scale and the weights (config.WEIGHTS) sum to
1.0, so the composite is itself 0-100 and directly comparable to the
MIN_TREND_SCORE_THRESHOLD.

  S_cred      mean Gate 1 credibility of the cluster's signals
  S_growth    mention velocity: recent half-window vs. older half-window
  S_recency   mean Gate 1 freshness decay, rescaled to 0-100
  S_cross     ladder: 2 platforms = 80, 3 = 90, 4+ = 100
  S_research  Gemini judgement against technical/research criteria
  S_r2c       Gemini judgement against R2C's business/audience fit
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from . import config, text_utils
from .gate2_cluster import TopicCluster
from .gemini_client import GeminiClient, get_client
from .ingest import parse_timestamp

logger = logging.getLogger("gate2.score")


@dataclass
class TrendScore:
    """Composite score plus the full breakdown that produced it."""

    cluster: TopicCluster
    credibility_score: float = 0.0
    growth_rate_score: float = 0.0
    recency_score: float = 0.0
    cross_platform_score: float = 0.0
    research_relevance_score: float = 0.0
    r2c_relevance_score: float = 0.0
    composite_trend_score: float = 0.0
    evidence: Dict[str, Any] = field(default_factory=dict)

    def breakdown(self) -> Dict[str, float]:
        return {
            "credibility_score": self.credibility_score,
            "growth_rate_score": self.growth_rate_score,
            "recency_score": self.recency_score,
            "cross_platform_score": self.cross_platform_score,
            "research_relevance_score": self.research_relevance_score,
            "r2c_relevance_score": self.r2c_relevance_score,
        }

    def weighted_contributions(self) -> Dict[str, float]:
        pairs = [
            ("credibility_score", "w_credibility"),
            ("growth_rate_score", "w_growth_rate"),
            ("recency_score", "w_recency"),
            ("cross_platform_score", "w_cross_platform"),
            ("research_relevance_score", "w_research"),
            ("r2c_relevance_score", "w_r2c"),
        ]
        return {
            metric: round(getattr(self, metric) * config.WEIGHTS[weight], 2)
            for metric, weight in pairs
        }


# ---------------------------------------------------------------------------
# Sub-score 1 — credibility
# ---------------------------------------------------------------------------
def score_credibility(cluster: TopicCluster) -> float:
    scores = [
        float(s["gate_1_metrics"].get("credibility_score") or 0.0) for s in cluster.signals
    ]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


# ---------------------------------------------------------------------------
# Sub-score 2 — growth rate / mention velocity
# ---------------------------------------------------------------------------
def score_growth_rate(cluster: TopicCluster, now: Optional[datetime] = None) -> tuple[float, Dict[str, Any]]:
    """
    Split the observation window in half and compare mention counts.

    An accelerating topic (more mentions recently than before) scores above 50;
    a decaying one scores below. Volume gives a small bonus so that a large
    steady conversation still outranks a tiny spiky one.
    """
    now = now or datetime.now(timezone.utc)
    half = config.GROWTH_WINDOW_HOURS / 2.0

    # Mentions are weighted by platform (config.PLATFORM_SIGNAL_WEIGHT): a news
    # article or a trending model counts for more than one short social post,
    # so a burst of Bluesky replies cannot manufacture velocity on its own.
    recent = older = 0.0
    recent_raw = older_raw = 0
    for signal in cluster.signals:
        posted = parse_timestamp(signal["source_attribution"].get("timestamp"))
        if posted is None:
            continue
        weight = config.PLATFORM_SIGNAL_WEIGHT.get(
            signal["source_attribution"].get("primary_platform"),
            config.DEFAULT_PLATFORM_SIGNAL_WEIGHT,
        )
        age_hours = (now - posted).total_seconds() / 3600.0
        if age_hours <= half:
            recent += weight
            recent_raw += 1
        elif age_hours <= config.GROWTH_WINDOW_HOURS:
            older += weight
            older_raw += 1

    if recent_raw + older_raw == 0:
        return 0.0, {"recent_mentions": 0, "older_mentions": 0, "note": "no timestamped mentions"}

    if older == 0:
        velocity = 100.0 if recent >= 2 else 75.0   # entirely new conversation
    else:
        ratio = recent / float(older)
        # ratio 1.0 (flat) -> 50; ratio >= 3.0 (tripling) -> 100
        velocity = max(0.0, min(100.0, 50.0 * ratio if ratio <= 2.0 else 100.0))

    volume_bonus = max(0.0, min(10.0, (recent + older - 2) * 2.5))
    score = round(max(0.0, min(100.0, velocity * 0.9 + volume_bonus)), 2)
    return score, {
        "recent_mentions": recent_raw,
        "older_mentions": older_raw,
        "recent_weighted": round(recent, 2),
        "older_weighted": round(older, 2),
        "window_hours": config.GROWTH_WINDOW_HOURS,
        "velocity_component": round(velocity, 2),
        "volume_bonus": round(volume_bonus, 2),
    }


# ---------------------------------------------------------------------------
# Sub-score 3 — recency
# ---------------------------------------------------------------------------
def score_recency(cluster: TopicCluster) -> float:
    """Mean Gate 1 freshness weight (0-1) rescaled to 0-100."""
    weights = [
        float(s["gate_1_metrics"].get("freshness_score") or 0.0) for s in cluster.signals
    ]
    if not weights:
        return 0.0
    return round((sum(weights) / len(weights)) * 100.0, 2)


# ---------------------------------------------------------------------------
# Sub-score 4 — cross-platform validation
# ---------------------------------------------------------------------------
def score_cross_platform(cluster: TopicCluster) -> float:
    count = len(cluster.platforms)
    if count >= 4:
        return config.CROSS_PLATFORM_SCORE_MAX
    return config.CROSS_PLATFORM_SCORE_LADDER.get(count, 0.0)


# ---------------------------------------------------------------------------
# Sub-scores 5 & 6 — semantic relevance via Gemini
# ---------------------------------------------------------------------------
RELEVANCE_PROMPT = """{criteria}

Topic title: {title}
Topic summary: {summary}
Representative signals:
{excerpts}

Rate how well this topic matches the criteria above on a scale of 0 to 100,
where 0 is entirely unrelated and 100 is a perfect match.
Reply with the number only - no words, no punctuation, no explanation."""

RESEARCH_KEYWORDS = {
    "research", "benchmark", "paper", "arxiv", "study", "dataset", "model",
    "evaluation", "experiment", "results", "method", "architecture", "training",
    "inference", "reproducible", "preprint", "peer", "ablation", "sota",
    "accuracy", "parameters", "open-source", "release", "framework",
}
R2C_KEYWORDS = {
    "agents", "agent", "llm", "llms", "ai", "ml", "machine", "learning", "model",
    "models", "pipeline", "automation", "rag", "embeddings", "inference",
    "fine-tuning", "prompt", "tooling", "infrastructure", "deployment",
    "production", "engineering", "data", "evaluation", "benchmark", "founders",
    "startup", "developers", "api", "opensource", "open-source",
}


def _excerpts(cluster: TopicCluster, limit: int = 5, chars: int = 240) -> str:
    return "\n".join(
        "- [{}] {}".format(
            signal["source_attribution"]["primary_platform"],
            signal["content"]["raw_text"][:chars].replace("\n", " "),
        )
        for signal in cluster.signals[:limit]
    )


def _keyword_relevance(cluster: TopicCluster, vocabulary: set) -> float:
    """Deterministic fallback when Gemini is unavailable."""
    tokens = set(text_utils.tokenize(" ".join(cluster.texts + [cluster.topic_title])))
    if not tokens:
        return 0.0
    hits = len(tokens & vocabulary)
    # 8+ distinct on-topic terms is treated as a strong match.
    return round(min(100.0, 25.0 + (hits / 8.0) * 75.0) if hits else 10.0, 2)


def score_research_relevance(
    cluster: TopicCluster, client: Optional[GeminiClient] = None
) -> tuple[float, str]:
    client = client or get_client()
    score = client.score_0_100(
        RELEVANCE_PROMPT.format(
            criteria=config.RESEARCH_CRITERIA,
            title=cluster.topic_title,
            summary=cluster.topic_summary,
            excerpts=_excerpts(cluster),
        ),
        label="research_relevance",
    )
    if score is None:
        return _keyword_relevance(cluster, RESEARCH_KEYWORDS), "keyword_fallback"
    return round(score, 2), "gemini"


def score_r2c_relevance(
    cluster: TopicCluster, client: Optional[GeminiClient] = None
) -> tuple[float, str]:
    client = client or get_client()
    score = client.score_0_100(
        RELEVANCE_PROMPT.format(
            criteria=config.R2C_DOMAIN_CONTEXT
            + "\nRate topical fit for this audience and business.",
            title=cluster.topic_title,
            summary=cluster.topic_summary,
            excerpts=_excerpts(cluster),
        ),
        label="r2c_relevance",
    )
    if score is None:
        return _keyword_relevance(cluster, R2C_KEYWORDS), "keyword_fallback"
    return round(score, 2), "gemini"


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def score_cluster(cluster: TopicCluster, client: Optional[GeminiClient] = None) -> TrendScore:
    client = client or get_client()
    result = TrendScore(cluster=cluster)

    result.credibility_score = score_credibility(cluster)
    result.growth_rate_score, growth_evidence = score_growth_rate(cluster)
    result.recency_score = score_recency(cluster)
    result.cross_platform_score = score_cross_platform(cluster)
    result.research_relevance_score, research_source = score_research_relevance(cluster, client)
    result.r2c_relevance_score, r2c_source = score_r2c_relevance(cluster, client)

    weights = config.WEIGHTS
    result.composite_trend_score = round(
        result.credibility_score * weights["w_credibility"]
        + result.growth_rate_score * weights["w_growth_rate"]
        + result.recency_score * weights["w_recency"]
        + result.cross_platform_score * weights["w_cross_platform"]
        + result.research_relevance_score * weights["w_research"]
        + result.r2c_relevance_score * weights["w_r2c"],
        2,
    )
    result.evidence = {
        "growth": growth_evidence,
        "platform_count": len(cluster.platforms),
        "platforms": cluster.platforms,
        "signal_count": cluster.size,
        "research_score_source": research_source,
        "r2c_score_source": r2c_source,
        "weights": dict(weights),
        "weighted_contributions": result.weighted_contributions(),
    }

    logger.info(
        "GATE2 | %s | cred %.1f | growth %.1f | recency %.1f | cross %.1f | research %.1f | r2c %.1f => COMPOSITE %.2f",
        cluster.cluster_id, result.credibility_score, result.growth_rate_score,
        result.recency_score, result.cross_platform_score,
        result.research_relevance_score, result.r2c_relevance_score,
        result.composite_trend_score,
    )
    return result


def score_clusters(
    clusters: Sequence[TopicCluster], client: Optional[GeminiClient] = None
) -> List[TrendScore]:
    logger.info("GATE2 | ---- composite trend scoring over %d cluster(s) ----", len(clusters))
    client = client or get_client()
    return [score_cluster(cluster, client) for cluster in clusters]
