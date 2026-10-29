"""
gate2_rank.py — GATE 2 (step 3): thresholding and ranking.

Applies MIN_TREND_SCORE_THRESHOLD (70.0), ranks the survivors in descending
composite order, and emits the Week 1 output contract: an array of Ranked
High-Value Trends marked QUALIFIED_FOR_WEEK_2_GENERATION.

This JSON is the handoff to Week 2 (content generation). Nothing downstream of
Gate 2 is implemented here - that is deliberate and in scope for Week 2.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config
from .gate2_score import TrendScore

logger = logging.getLogger("gate2.rank")

STATUS_QUALIFIED = "QUALIFIED_FOR_WEEK_2_GENERATION"
STATUS_BELOW_THRESHOLD = "DROPPED_BELOW_THRESHOLD"
STATUS_OFF_BRAND = "DROPPED_OFF_BRAND"

SOURCE_EXCERPT_CHARS = 600


def make_trend_id(rank: int, cluster_id: str, title: str) -> str:
    """Stable, readable id: trd_<YYYYMMDD>_<NNN>."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    digest = hashlib.md5("{}|{}".format(cluster_id, title).encode("utf-8")).hexdigest()[:4]
    return "trd_{}_{:03d}_{}".format(stamp, rank, digest)


def build_trend_record(
    score: TrendScore,
    rank: int,
    minimum_threshold: Optional[float] = None,
) -> Dict[str, Any]:
    cluster = score.cluster
    platforms = cluster.platforms
    applied_threshold = (
        config.MIN_TREND_SCORE_THRESHOLD
        if minimum_threshold is None
        else minimum_threshold
    )
    return {
        "trend_id": make_trend_id(rank, cluster.cluster_id, cluster.topic_title),
        "rank": rank,
        "topic_title": cluster.topic_title,
        "topic_summary": cluster.topic_summary,
        "composite_trend_score": score.composite_trend_score,
        "minimum_threshold_applied": applied_threshold,
        "scoring_breakdown": score.breakdown(),
        "scoring_weights": dict(config.WEIGHTS),
        "weighted_contributions": score.weighted_contributions(),
        "cross_platform_validation": {
            "platform_count": len(platforms),
            "platforms": platforms,
            "meets_minimum_rule": len(platforms) >= config.MIN_CROSS_PLATFORM_COUNT,
            "minimum_required": config.MIN_CROSS_PLATFORM_COUNT,
        },
        "growth_evidence": score.evidence.get("growth", {}),
        "topic_keywords": cluster.keywords,
        "topic_entities": cluster.entities,
        "topic_key_facts": cluster.key_facts,
        "supporting_signal_count": cluster.size,
        "supporting_signal_ids": cluster.signal_ids,
        # Every source carries its headline and lead so content generation can
        # quote real evidence instead of a two-sentence summary.
        "source_attribution": [
            {
                "signal_id": signal["signal_id"],
                "platform": signal["source_attribution"]["primary_platform"],
                "source_url": signal["source_attribution"]["source_url"],
                "author_id": signal["source_attribution"]["author_id"],
                "timestamp": signal["source_attribution"]["timestamp"],
                "credibility_score": signal["gate_1_metrics"]["credibility_score"],
                "source_insight": " ".join(
                    (signal["content"].get("title_or_summary") or "").split())[:200],
                "excerpt": " ".join(
                    (signal["content"].get("raw_text") or "").split())[:SOURCE_EXCERPT_CHARS],
            }
            for signal in cluster.signals
        ],
        "cluster_id": cluster.cluster_id,
        "status": STATUS_QUALIFIED,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def drop_reason(
    score: TrendScore, threshold: float, minimum_r2c: float
) -> Optional[Tuple[str, str]]:
    """(status, reason) if the cluster must be dropped, else None."""
    if score.composite_trend_score < threshold:
        return STATUS_BELOW_THRESHOLD, "composite {:.2f} < {:.1f}".format(
            score.composite_trend_score, threshold)
    if score.r2c_relevance_score < minimum_r2c:
        return STATUS_OFF_BRAND, "R2C relevance {:.0f} < {:.0f}: poor fit for the audience".format(
            score.r2c_relevance_score, minimum_r2c)
    return None


def rank_trends(
    scores: Sequence[TrendScore],
    minimum_score: Optional[float] = None,
    minimum_r2c: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Split scored clusters into (qualified ranked trends, dropped clusters)."""
    threshold = (
        config.MIN_TREND_SCORE_THRESHOLD
        if minimum_score is None
        else minimum_score
    )
    r2c_floor = config.MIN_R2C_RELEVANCE_SCORE if minimum_r2c is None else minimum_r2c
    logger.info(
        "GATE2 | ---- thresholding at %.1f (R2C fit >= %.0f) and ranking %d cluster(s) ----",
        threshold, r2c_floor, len(scores),
    )

    reasons = {id(s): drop_reason(s, threshold, r2c_floor) for s in scores}
    qualified = [s for s in scores if reasons[id(s)] is None]
    dropped_scores = [s for s in scores if reasons[id(s)] is not None]
    qualified.sort(key=lambda s: s.composite_trend_score, reverse=True)

    trends: List[Dict[str, Any]] = []
    for position, score in enumerate(qualified, start=1):
        record = build_trend_record(score, position, minimum_threshold=threshold)
        trends.append(record)
        logger.info(
            "GATE2 | RANK %d | %.2f | %s | %d signals across %s",
            position, score.composite_trend_score, record["topic_title"],
            score.cluster.size, record["cross_platform_validation"]["platforms"],
        )

    dropped: List[Dict[str, Any]] = []
    for score in sorted(dropped_scores, key=lambda s: s.composite_trend_score, reverse=True):
        cluster = score.cluster
        status, reason = reasons[id(score)]
        near_miss = (
            status == STATUS_BELOW_THRESHOLD
            and score.composite_trend_score >= threshold - config.NEAR_MISS_MARGIN
            and score.r2c_relevance_score >= r2c_floor
        )
        dropped.append(
            {
                "cluster_id": cluster.cluster_id,
                "topic_title": cluster.topic_title,
                "topic_summary": cluster.topic_summary,
                "topic_entities": cluster.entities,
                "topic_key_facts": cluster.key_facts,
                "composite_trend_score": score.composite_trend_score,
                "minimum_threshold_applied": threshold,
                "minimum_r2c_relevance_applied": r2c_floor,
                "scoring_breakdown": score.breakdown(),
                "platforms": cluster.platforms,
                "supporting_signal_count": cluster.size,
                "supporting_signal_ids": cluster.signal_ids,
                "source_urls": [
                    s["source_attribution"].get("source_url") for s in cluster.signals
                ],
                "status": status,
                "drop_reason": reason,
                "near_miss": near_miss,
            }
        )
        logger.info("GATE2 | DROPPED | %s | %s", reason, cluster.topic_title)

    off_brand = sum(1 for d in dropped if d["status"] == STATUS_OFF_BRAND)
    logger.info(
        "GATE2 | ranking complete: %d QUALIFIED, %d dropped below threshold, %d dropped as off-brand",
        len(trends), len(dropped) - off_brand, off_brand,
    )
    return trends, dropped


def run_ranking(scores: Sequence[TrendScore]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    trends, dropped = rank_trends(scores)
    from . import storage  # local import keeps the module import graph acyclic

    storage.save_ranked_trends(trends)
    storage.write_json(config.DROPPED_TRENDS_JSON, dropped)
    return trends, dropped
