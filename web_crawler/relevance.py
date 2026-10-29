"""
relevance.py — Semantic relevance gate for crawled records.

The keyword regex in settings.RELEVANCE_KEYWORDS decides what is worth
FETCHING. It cannot tell a research paper from a press release that mentions
"AI" once, and it lets through jokes that contain "LLM". This pass runs after
the crawl: every record is scored 0-100 by Gemini on how central AI/ML is to
it, in batches, and records below settings.MIN_AI_RELEVANCE are dropped before
the trend pipeline sees them. The score is kept on each record as
`ai_relevance` for auditing.

Fail-open by design: without Gemini, or if a batch call fails, that batch is
kept unchanged (the pipeline's Gate 2 relevance scoring still applies later),
and the stats say so.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from trend_pipeline import ingest
from trend_pipeline.gemini_client import GeminiClient, get_client

from . import settings

logger = logging.getLogger("web_crawler.relevance")

ITEM_CHARS = 400

RELEVANCE_PROMPT = """You are screening crawled items for a newsletter about AI and machine learning research and its path to industry.

For each numbered item, rate 0-100 how centrally it is about artificial intelligence or machine learning: models and model releases, research papers, benchmarks, agents, AI tools and infrastructure, AI companies' product or business news, or AI policy.
  90-100: entirely about a specific AI/ML development, with substance (names, numbers, findings).
  60-80: AI/ML is the main subject but the item is thin, general, or opinion.
  30-50: AI/ML is one topic among others, or the item is about something that merely uses AI.
  0-20: AI appears only in passing, as a joke or figure of speech, or not at all (sports, weather, general politics, spam).
Judge the item itself, not how important the topic is.

Items:
{items}

Reply with JSON only, mapping every item number to its score, for example {{"1": 85, "2": 10}}."""

_PAIR_RE = re.compile(r'"?(\d+)"?\s*:\s*(\d{1,3})')


def record_text(platform: str, record: Dict[str, Any]) -> str:
    """Reuse the ingest adapters so every platform's native fields are read correctly."""
    signal = ingest.normalize_record(record, platform)
    text = signal.text if signal is not None else json.dumps(record, ensure_ascii=False)
    return " ".join(text.split())[:ITEM_CHARS]


def parse_scores(reply: Optional[str], count: int) -> Dict[int, int]:
    """Parse {"1": 85, ...}; tolerate prose around the JSON and clamp to 0-100."""
    if not reply:
        return {}
    scores: Dict[int, int] = {}
    start, end = reply.find("{"), reply.rfind("}")
    parsed: Any = None
    if start >= 0 and end > start:
        try:
            parsed = json.loads(reply[start:end + 1])
        except ValueError:
            parsed = None
    pairs = parsed.items() if isinstance(parsed, dict) else _PAIR_RE.findall(reply)
    for key, value in pairs:
        try:
            index, score = int(str(key).strip()), int(float(value))
        except (TypeError, ValueError):
            continue
        if 1 <= index <= count:
            scores[index] = max(0, min(100, score))
    return scores


def score_batch(
    items: Sequence[Tuple[str, Dict[str, Any]]], client: GeminiClient
) -> Optional[Dict[int, int]]:
    """Score one batch; None means the call failed and the batch should be kept."""
    lines = "\n".join(
        "{}. [{}] {}".format(index, platform, record_text(platform, record))
        for index, (platform, record) in enumerate(items, start=1)
    )
    reply = client.generate_text(RELEVANCE_PROMPT.format(items=lines), label="crawl_relevance")
    scores = parse_scores(reply, len(items))
    if len(scores) < len(items) * 0.8:
        logger.warning("CRAWL | relevance batch returned %d/%d scores; keeping the batch",
                       len(scores), len(items))
        return None
    return scores


def filter_payload(
    payload: Dict[str, List[Dict[str, Any]]],
    client: Optional[GeminiClient] = None,
    minimum: Optional[int] = None,
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], Dict[str, int]]:
    """
    Return (kept_payload, dropped_records, stats).

    Dropped records carry their platform and score so a cycle's off-topic
    set can be archived next to the crawl for review.
    """
    client = client or get_client()
    minimum = settings.MIN_AI_RELEVANCE if minimum is None else minimum
    total = sum(len(records) for records in payload.values())
    stats: Dict[str, int] = {"relevance scored": 0, "off-topic dropped": 0, "relevance unscored": 0}
    if not total:
        return payload, [], stats
    if not client.online:
        logger.info("CRAWL | relevance gate skipped (Gemini offline); keeping all %d records", total)
        stats["relevance unscored"] = total
        return payload, [], stats

    flat: List[Tuple[str, Dict[str, Any]]] = [
        (platform, record) for platform, records in payload.items() for record in records
    ]
    kept: Dict[str, List[Dict[str, Any]]] = {platform: [] for platform in payload}
    dropped: List[Dict[str, Any]] = []
    for start in range(0, len(flat), settings.RELEVANCE_BATCH_SIZE):
        batch = flat[start:start + settings.RELEVANCE_BATCH_SIZE]
        scores = score_batch(batch, client)
        for index, (platform, record) in enumerate(batch, start=1):
            if scores is None:
                stats["relevance unscored"] += 1
                kept[platform].append(record)
                continue
            score = scores.get(index)
            if score is None:
                stats["relevance unscored"] += 1
                kept[platform].append(record)
                continue
            record["ai_relevance"] = score
            stats["relevance scored"] += 1
            if score >= minimum:
                kept[platform].append(record)
            else:
                stats["off-topic dropped"] += 1
                dropped.append({"platform": platform, "ai_relevance": score,
                                "text": record_text(platform, record)[:200], "record": record})
    kept = {platform: records for platform, records in kept.items() if records}
    logger.info("CRAWL | relevance gate: %d scored, %d dropped below %d, %d kept unscored",
                stats["relevance scored"], stats["off-topic dropped"], minimum,
                stats["relevance unscored"])
    return kept, dropped, stats
