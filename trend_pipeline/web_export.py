"""
web_export.py — Compact trend records for the web app.

The UI shows title, summary, score, platforms and sources; ad_creatives needs the
summary, key facts, entities and source excerpts to write an ad brief. Everything
else in a ranked-trend record stays out so payloads stay small.
"""
from __future__ import annotations

from typing import Any, Dict


def slim_trend(trend: Dict[str, Any], near_miss: bool = False) -> Dict[str, Any]:
    cpv = trend.get("cross_platform_validation") or {}
    scores = trend.get("scoring_breakdown") or {}
    sources = []
    for s in (trend.get("source_attribution") or [])[:6]:
        if not isinstance(s, dict):
            continue
        sources.append({
            "platform": s.get("platform"),
            "source_url": s.get("source_url"),
            "excerpt": str(s.get("excerpt") or s.get("source_insight") or "")[:450],
            "source_insight": str(s.get("source_insight") or "")[:300],
        })
    trend_id = trend.get("trend_id") or "near_{}".format(trend.get("cluster_id"))
    return {
        "trend_id": trend_id,
        "rank": trend.get("rank"),
        "near_miss": near_miss,
        "topic_title": trend.get("topic_title"),
        "topic_summary": trend.get("topic_summary"),
        "topic_key_facts": (trend.get("topic_key_facts") or [])[:6],
        "topic_entities": (trend.get("topic_entities") or [])[:10],
        "score": round(float(trend.get("composite_trend_score") or 0), 1),
        "r2c_relevance": scores.get("r2c_relevance_score"),
        "growth": scores.get("growth_rate_score"),
        "platforms": cpv.get("platforms") or sorted({s["platform"] for s in sources if s.get("platform")}),
        "generated_at": trend.get("generated_at"),
        "source_attribution": sources,
    }
