"""
autopilot.py — Run the crawler and the trend pipeline unattended.

Each cycle:
    1. pick queries   - the seed list plus the previous cycle's top trend titles,
                        so the crawler follows stories it has just found
    2. crawl          - web_crawler.crawler (Crawlee) -> {platform: [records]}
    3. save           - data/input/crawled_signals.json (+ timestamped archive)
    4. Gate 1 + 2     - trend_pipeline.main.run_pipeline, WITHOUT reset, so the
                        post-history window skips anything seen in earlier cycles
    5. generate       - optional: AIDA posts and image prompts for this cycle's trends,
                        written to data/output/autopilot/<run_id>/

Usage:
    python -m web_crawler                         # one cycle, stop at ranked trends
    python -m web_crawler --generate              # one cycle + campaign copy and prompts
    python -m web_crawler --loop --interval 60    # autonomous, every 60 minutes
    python -m web_crawler --crawl-only            # just crawl and save the JSON
    python -m web_crawler --query "AI chips" --query "robotics"
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from trend_pipeline import config, storage
from trend_pipeline.main import run_pipeline, setup_logging

from . import crawler, settings

logger = logging.getLogger("autopilot")

ADAPTIVE_QUERY_COUNT = 3
ADAPTIVE_QUERY_WORDS = 6


def next_cycle_queries(base: Sequence[str], adaptive: bool = True) -> List[str]:
    """Seed queries plus short forms of the last cycle's top-ranked trend titles."""
    queries = list(base)
    if not adaptive:
        return queries
    try:
        with open(config.RANKED_TRENDS_JSON, "r", encoding="utf-8") as handle:
            trends = json.load(handle)
    except (OSError, ValueError):
        return queries
    for trend in (trends or [])[:ADAPTIVE_QUERY_COUNT]:
        words = re.sub(r"[^\w\s\-]", " ", trend.get("topic_title", "")).split()
        query = " ".join(words[:ADAPTIVE_QUERY_WORDS])
        if query and query.lower() not in {q.lower() for q in queries}:
            queries.append(query)
    return queries


def save_payload(payload: Dict[str, List[Dict[str, Any]]], run_id: str) -> str:
    os.makedirs(settings.CRAWL_ARCHIVE_DIR, exist_ok=True)
    archive = os.path.join(settings.CRAWL_ARCHIVE_DIR, "crawl_{}.json".format(run_id))
    for path in (settings.CRAWLED_INPUT_JSON, archive):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    return archive


def save_off_topic(dropped: List[Dict[str, Any]], run_id: str) -> str:
    """Archive what the relevance gate removed, so its decisions can be reviewed."""
    os.makedirs(settings.CRAWL_ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(settings.CRAWL_ARCHIVE_DIR, "crawl_{}_offtopic.json".format(run_id))
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dropped, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def generate_content(run_id: str, stage: Optional[str]) -> Dict[str, Any]:
    """AIDA posts for every ranked trend, then one image per post at its stage's aspect ratio."""
    from image_generator import generate_images
    from prompt_generator import generate_prompts

    run_dir = os.path.join(config.DATA_DIR, "autopilot", run_id)
    os.makedirs(run_dir, exist_ok=True)
    prompts_path = os.path.join(run_dir, "generated_prompts.json")
    ads_path = os.path.join(run_dir, "generated_ads.json")
    posts = generate_prompts.run(
        input_path=config.RANKED_TRENDS_JSON, output_path=prompts_path, stage=stage)
    ok_posts = sum(1 for p in posts if p.get("status") == "success")
    images: List[Dict[str, Any]] = []
    if ok_posts:
        images = generate_images.run(
            input_path=prompts_path,
            output_path=ads_path,
            images_dir=os.path.join(run_dir, "images"),
        )
    return {
        "output_dir": run_dir,
        "prompts_path": prompts_path,
        "ads_path": ads_path,
        "posts_ok": ok_posts,
        "posts_total": len(posts),
        "images_ok": sum(1 for i in images if i.get("status") == "success"),
        "images_total": len(images),
    }


def run_cycle(
    queries: Sequence[str],
    adaptive: bool = True,
    use_radar: bool = True,
    use_relevance_gate: bool = True,
    crawl_only: bool = False,
    generate: bool = False,
    stage: Optional[str] = None,
) -> Dict[str, Any]:
    started = datetime.now(timezone.utc)
    run_id = started.strftime("%Y%m%dT%H%M%SZ")
    cycle_queries = next_cycle_queries(queries, adaptive=adaptive)
    summary: Dict[str, Any] = {"run_id": run_id, "started_at": started.isoformat(),
                               "queries": cycle_queries}

    result = crawler.crawl(cycle_queries, use_radar=use_radar, use_relevance_gate=use_relevance_gate)
    summary["queries"] = result.queries
    summary["trending_now"] = [
        {"query": query, "origin": origin} for query, origin in result.trending_queries]
    payload = result.payload()
    summary["crawl"] = dict(result.stats)
    summary["records"] = {p: len(r) for p, r in payload.items()}
    summary["crawl_file"] = save_payload(payload, run_id)
    if result.off_topic:
        summary["off_topic_file"] = save_off_topic(result.off_topic, run_id)
    logger.info("AUTOPILOT | crawl saved: %s | %s", summary["crawl_file"], summary["records"])

    if crawl_only:
        pass
    elif not payload:
        logger.warning("AUTOPILOT | crawl produced no records; skipping the pipeline")
    else:
        trends = run_pipeline(payload, print_json=False)
        summary["trends"] = [
            {"rank": t["rank"], "title": t["topic_title"],
             "score": t["composite_trend_score"],
             "platforms": t["cross_platform_validation"]["platforms"]}
            for t in trends
        ]
        if generate and trends:
            try:
                summary["generation"] = generate_content(run_id, stage)
            except (OSError, ValueError, RuntimeError) as exc:
                logger.error("AUTOPILOT | generation failed: %s", exc)
                summary["generation"] = {"error": str(exc)}

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(settings.AUTOPILOT_LOG_JSONL), exist_ok=True)
    with open(settings.AUTOPILOT_LOG_JSONL, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False) + "\n")
    return summary


def print_summary(summary: Dict[str, Any]) -> None:
    print("\n" + "=" * 78)
    print("AUTOPILOT CYCLE {}".format(summary["run_id"]).center(78))
    print("=" * 78)
    trending_now = summary.get("trending_now") or []
    print("  Trending  : {}".format(
        "; ".join("{} [{}]".format(t["query"], t["origin"]) for t in trending_now[:12])
        or "(no AI-relevant trend found by the radar)"))
    print("  Queries   : {}".format(", ".join(summary["queries"])))
    print("  Records   : {}".format(summary.get("records")))
    print("  Crawl     : {}".format(summary.get("crawl")))
    for trend in summary.get("trends", []):
        print("  #{rank} [{score:.1f}] {title}  ({platforms})".format(
            rank=trend["rank"], score=trend["score"], title=trend["title"],
            platforms=", ".join(trend["platforms"])))
    if "trends" in summary and not summary["trends"]:
        print("  No trend qualified this cycle.")
    if summary.get("generation"):
        print("  Generation: {}".format(summary["generation"]))
    print("  Logged to : {}".format(settings.AUTOPILOT_LOG_JSONL))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="R2C.ai autonomous crawler: discover -> Gate 1/2 -> (optional) campaign copy")
    parser.add_argument("--query", action="append", metavar="TEXT",
                        help="Search query (repeatable). Replaces the seed list in settings.py")
    parser.add_argument("--loop", action="store_true", help="Keep running cycles forever")
    parser.add_argument("--interval", type=float, default=60.0,
                        help="Minutes between cycle starts with --loop (default 60)")
    parser.add_argument("--crawl-only", action="store_true",
                        help="Only crawl and save data/input/crawled_signals.json")
    parser.add_argument("--generate", action="store_true",
                        help="Also generate AIDA posts and image prompts for qualified trends")
    parser.add_argument("--stage", choices=("awareness", "interest", "desire", "action"),
                        help="With --generate, produce one AIDA stage only (default: all four)")
    parser.add_argument("--no-adaptive", action="store_true",
                        help="Do not add the previous cycle's trend titles as queries")
    parser.add_argument("--no-radar", action="store_true",
                        help="Skip the trend radar (Google Trends, Bluesky, Mastodon, HF trending)")
    parser.add_argument("--no-relevance-gate", action="store_true",
                        help="Skip the Gemini relevance scoring of crawled records (keyword filter only)")
    parser.add_argument("--reset", action="store_true",
                        help="Clear stored artefacts and dedup history before the first cycle")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    if not args.verbose:
        # Blocked/paywalled pages are routine on the open web; Crawlee logs each
        # retry with a traceback. They are still counted in the cycle stats.
        logging.getLogger("crawlee").setLevel(logging.CRITICAL)
    if args.reset:
        storage.reset_store(remove_history=True)
    queries = args.query or settings.SEED_QUERIES

    while True:
        cycle_start = time.monotonic()
        try:
            summary = run_cycle(queries, adaptive=not args.no_adaptive,
                                use_radar=not args.no_radar,
                                use_relevance_gate=not args.no_relevance_gate, crawl_only=args.crawl_only, generate=args.generate,
                                stage=args.stage)
            print_summary(summary)
        except KeyboardInterrupt:
            return 130
        except Exception:  # noqa: BLE001 - an unattended loop must survive one bad cycle
            logger.exception("AUTOPILOT | cycle failed")
            if not args.loop:
                return 1
        if not args.loop:
            return 0
        wait = max(0.0, args.interval * 60 - (time.monotonic() - cycle_start))
        logger.info("AUTOPILOT | next cycle in %.0f min (Ctrl+C to stop)", wait / 60)
        try:
            time.sleep(wait)
        except KeyboardInterrupt:
            return 130


if __name__ == "__main__":
    raise SystemExit(main())
