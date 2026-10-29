"""
main.py — Week 1 pipeline orchestration.

    RESEARCH -> COLLECT SIGNALS -> VALIDATE (GATE 1) -> CLEAN/VALIDATED SIGNALS
    -> CLUSTER TOPICS -> SCORE TRENDS (GATE 2) -> RANKED HIGH-VALUE TRENDS

JSON and demo runs stop at ranked trends. A manually submitted --url continues
through AIDA post generation and image generation (Cloudflare Workers AI).

Usage:
    python main.py --reset                          # clean demo run on the built-in fixture
    python main.py                                  # next cycle: already-seen posts are skipped
    python main.py --url https://example.com/post   # scrape and write an action post
    python main.py --url URL1 URL2 URL3             # scrape multiple URLs in one run
    python main.py --input raw.json                 # run on {platform: [records]} JSON
    python main.py --verbose                        # DEBUG-level grouping/clustering decisions

Re-running WITHOUT --reset is the idempotence check: resurfaced historical
signals are revalidated so they can corroborate current topics, while storage
upserts by signal ID to avoid duplicate records.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import (
    config,
    gate1_cross,
    gate1_quality,
    gate2_cluster,
    gate2_rank,
    gate2_score,
    ingest,
    storage,
    url_scraper,
)
from .gemini_client import get_client
from .url_scraper import scrape_url

logger = logging.getLogger("main")

BANNER_WIDTH = 78


def setup_logging(verbose: bool = False) -> None:
    # Scraped social content routinely contains emoji and accented text. On a
    # Windows console (cp1252 by default) printing it raises UnicodeEncodeError,
    # so force UTF-8 on the streams before any logging is configured.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # non-reconfigurable stream (e.g. a pipe)
                pass

    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
        stream=sys.stdout,
    )


def banner(text: str) -> None:
    print("\n" + "=" * BANNER_WIDTH)
    print(text.center(BANNER_WIDTH))
    print("=" * BANNER_WIDTH)


def section(text: str) -> None:
    print("\n" + "-" * BANNER_WIDTH)
    print(text)
    print("-" * BANNER_WIDTH)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def print_gate1_summary(
    total_ingested: int,
    verdicts: Sequence[gate1_quality.QualityVerdict],
    validated: Sequence[gate1_quality.QualityVerdict],
) -> None:
    # `verdicts` already reflects cross-platform rejections: gate1_cross updates
    # each verdict in place (status REJECT_SINGLE_SOURCE), so counting it alone
    # avoids double-counting.
    section("GATE 1 - DATA QUALITY SUMMARY")
    counts = gate1_quality.rejection_summary(verdicts)
    labels = {
        gate1_quality.REJECT_SPAM: "Spam / bot",
        gate1_quality.REJECT_DUPLICATE: "Duplicate / near-duplicate",
        gate1_quality.REJECT_ALREADY_PROCESSED: "Already processed (earlier cycle)",
        gate1_quality.REJECT_LOW_CREDIBILITY: "Low credibility",
        gate1_quality.REJECT_STALE: "Stale (past freshness cutoff)",
        gate1_quality.REJECT_UNATTRIBUTED: "Missing source attribution",
        gate1_quality.REJECT_SINGLE_SOURCE: "Single-source (< 2 platforms)",
    }
    print("  Signals ingested                : {}".format(total_ingested))
    for status, label in labels.items():
        print("  Rejected — {:<30}: {}".format(label, counts.get(status, 0)))
    rejected_total = sum(counts.values())
    print("  {:<43}: {}".format("TOTAL REJECTED", rejected_total))
    print("  {:<43}: {}".format("VALIDATED SIGNALS", len(validated)))
    if total_ingested:
        print("  {:<43}: {:.1f}%".format(
            "Validation pass rate", 100.0 * len(validated) / total_ingested))


def print_trends(trends: Sequence[Dict[str, Any]], dropped: Sequence[Dict[str, Any]]) -> None:
    section("GATE 2 — RANKED HIGH-VALUE TRENDS")
    if not trends:
        print("  No topic cluster reached the minimum score of {:.1f}.".format(
            config.MIN_TREND_SCORE_THRESHOLD))
    for trend in trends:
        cross = trend["cross_platform_validation"]
        breakdown = trend["scoring_breakdown"]
        print("\n  RANK {} — {}  [{:.2f} / 100]".format(
            trend["rank"], trend["topic_title"], trend["composite_trend_score"]))
        print("    {}".format(trend["topic_summary"]))
        print("    platforms ({}): {}".format(cross["platform_count"], ", ".join(cross["platforms"])))
        print("    signals: {} | {}".format(
            trend["supporting_signal_count"], ", ".join(trend["supporting_signal_ids"][:4])))
        print("    credibility {:.1f} | growth {:.1f} | recency {:.1f} | "
              "cross-platform {:.1f} | research {:.1f} | r2c {:.1f}".format(
                  breakdown["credibility_score"], breakdown["growth_rate_score"],
                  breakdown["recency_score"], breakdown["cross_platform_score"],
                  breakdown["research_relevance_score"], breakdown["r2c_relevance_score"]))
        if trend.get("topic_entities"):
            print("    entities: {}".format(", ".join(trend["topic_entities"])))
        for fact in trend.get("topic_key_facts", [])[:3]:
            print("    fact: {}".format(fact))
        print("    status: {}".format(trend["status"]))
    if dropped:
        print("\n  Dropped (score < {:.1f}, or R2C fit < {:.0f}):".format(
            config.MIN_TREND_SCORE_THRESHOLD, config.MIN_R2C_RELEVANCE_SCORE))
        for item in dropped:
            print("    - {:.2f}  {}  [{}]".format(
                item["composite_trend_score"], item["topic_title"],
                item.get("drop_reason") or item["status"]))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    raw_payload: Dict[str, List[Dict[str, Any]]],
    print_json: bool = True,
    user_submitted_url: bool = False,
) -> List[Dict[str, Any]]:
    config.validate_config()
    client = get_client()

    banner("R2C.ai SOCIAL MEDIA AUTOMATION v2 — WEEK 1 PIPELINE")
    print("  Gemini mode            : {}".format(client.stats()["mode"]))
    print("  Cross-platform minimum : {} platforms".format(config.MIN_CROSS_PLATFORM_COUNT))
    print("  Trend score threshold  : {:.1f} / 100".format(config.MIN_TREND_SCORE_THRESHOLD))

    # -- 1. RESEARCH / COLLECT ------------------------------------------
    section("STEP 1 — RESEARCH: COLLECT & NORMALISE SIGNALS")
    signals = ingest.ingest(raw_payload)
    if not signals:
        logger.error("No signals ingested — aborting.")
        return []

    # -- 2. GATE 1 -------------------------------------------------------
    section("STEP 2 — GATE 1: DATA QUALITY VALIDATION")
    history = [] if user_submitted_url else storage.load_post_history()
    verdicts = gate1_quality.run_quality_checks(signals, post_history=history)
    survivors = gate1_quality.passing_verdicts(verdicts)

    clusters: List[gate2_cluster.TopicCluster] = []
    if user_submitted_url:
        validated = survivors
        for verdict in validated:
            verdict.cross_platform_confirmed = False
            verdict.platforms_detected = [verdict.signal.source_platform]
            verdict.corroboration_group = "user_submitted_url"
            verdict.user_selected_source_exception = True
        logger.info(
            "URL input is user-selected: continuing without automated cross-platform corroboration."
        )
    else:
        # Part B runs on Gate 2's semantic topic clusters, so "seen on two
        # platforms" is decided on the same clusters that are later scored.
        section("STEP 2b — GATE 1: CROSS-PLATFORM CORROBORATION ON TOPIC CLUSTERS")
        preview_records = [storage.build_validated_record(v) for v in survivors]
        clusters = gate2_cluster.build_clusters(preview_records, client=client)
        validated, _cross_rejected, clusters = gate1_cross.run_cross_platform_validation(
            survivors, clusters
        )
    print_gate1_summary(len(signals), verdicts, validated)

    # -- 3. STORE --------------------------------------------------------
    section("STEP 3 - STORE VALIDATED SIGNALS")
    validated_records = storage.save_validated_signals(validated)
    storage.save_rejected_signals([v for v in verdicts if not v.passed])
    if not user_submitted_url:
        storage.append_post_history(validated_records)
    if not validated_records:
        logger.error("Gate 1 validated nothing — Gate 2 cannot run.")
        return []

    # -- 4. GATE 2 -------------------------------------------------------
    section("STEP 4 — GATE 2: TOPIC CLUSTERING")
    if user_submitted_url:
        clusters = gate2_cluster.run_clustering(
            validated_records,
            client=client,
            minimum_cluster_size=1,
            require_cross_platform=False,
        )
    else:
        # Clusters were built in step 2b; attach the stored records and summarise.
        gate2_cluster.refresh_cluster_records(clusters, validated_records)
        gate2_cluster.summarise_clusters(clusters, client=client)
    if not clusters:
        logger.error("No topic clusters formed — no trends to score.")
        return []

    section("STEP 5 — GATE 2: COMPOSITE TREND SCORING")
    scores = gate2_score.score_clusters(clusters, client=client)

    section("STEP 6 — GATE 2: THRESHOLD & RANK")
    if user_submitted_url:
        trends, dropped = gate2_rank.rank_trends(scores, minimum_score=0.0, minimum_r2c=0.0)
        for trend in trends:
            trend["status"] = "USER_SUBMITTED_URL"
            trend["qualification_mode"] = "user_selected_source_exception"
            trend["cross_platform_validation"]["user_selected_source_exception"] = True
        storage.save_ranked_trends(trends)
    else:
        trends, dropped = gate2_rank.run_ranking(scores)

    # -- 5. OUTPUT -------------------------------------------------------
    print_trends(trends, dropped)

    if print_json:
        section("WEEK 1 OUTPUT — RANKED HIGH-VALUE TRENDS (JSON)")
        print(json.dumps(trends, indent=2, ensure_ascii=False))

    final_label = (
        "USER-SUBMITTED URL(S) PROCESSED"
        if user_submitted_url
        else "TREND(S) QUALIFIED FOR WEEK 2 GENERATION"
    )
    banner("WEEK 1 COMPLETE — {} {}".format(len(trends), final_label))
    print("  Validated signals : {}".format(config.VALIDATED_SIGNALS_JSON))
    print("  Rejected signals  : {}".format(config.REJECTED_SIGNALS_JSON))
    print("  Ranked trends     : {}".format(config.RANKED_TRENDS_JSON))
    print("  SQLite store      : {}".format(config.SIGNALS_DB))
    print("  Gemini calls      : {}".format(client.stats()))
    return trends


def load_urls_payload(urls: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """Scrape one or more URLs and merge them into a single platform-keyed payload."""
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for url in urls:
        logger.info("Scraping URL: %s", url)
        try:
            result = url_scraper.scrape_url(url)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Could not scrape %s: %s", url, exc)
            continue
        for platform, records in result.items():
            merged.setdefault(platform, []).extend(records)
    if not merged:
        raise ValueError("None of the provided URLs could be scraped successfully.")
    return merged


def load_raw_payload(
    path: Optional[str],
    urls: Optional[List[str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    # Priority: --url  >  --input  >  built-in sample fixture
    if urls:
        return load_urls_payload(urls)
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("Input JSON must be an object mapping platform -> [records].")
        return payload
    from . import sample_data

    logger.info("No --input or --url given; using the built-in demo fixture.")
    return sample_data.build_sample_raw_data()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="R2C.ai — scrape a public URL or run the JSON trend pipeline"
    )
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument(
        "--url",
        nargs="+",
        metavar="URL",
        help="One or more public webpage/post URLs; generate an AIDA post and image prompt",
    )
    input_group.add_argument(
        "--input",
        help="Raw scraper JSON: {platform: [records]}",
    )
    parser.add_argument(
        "--stage",
        choices=("awareness", "interest", "desire", "action"),
        default="action",
        help="AIDA stage generated for --url (default: action)",
    )
    parser.add_argument("--reset", action="store_true",
                        help="Clear stored artefacts and the dedup history for a clean run")
    parser.add_argument("--keep-history", action="store_true",
                        help="With --reset, keep the dedup post-history window")
    parser.add_argument("--no-json", action="store_true", help="Suppress the final JSON dump")
    parser.add_argument("--verbose", action="store_true", help="DEBUG logging")
    args = parser.parse_args(argv)

    setup_logging(args.verbose)

    if args.reset:
        storage.reset_store(remove_history=not args.keep_history)

    try:
        payload = load_raw_payload(args.input, urls=args.url)
        if args.url:
            scraped_path = os.path.join(config.DATA_DIR, "scraped_url_input.json")
            os.makedirs(os.path.dirname(scraped_path), exist_ok=True)
            with open(scraped_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            logger.info("Scraped page input saved to %s", scraped_path)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Could not load input: %s", exc)
        return 2

    trends = run_pipeline(
        payload,
        print_json=not args.no_json,
        user_submitted_url=bool(args.url),
    )
    if args.url and trends:
        try:
            from prompt_generator import generate_prompts

            prompt_records = generate_prompts.run(
                input_path=config.RANKED_TRENDS_JSON,
                output_path=config.GENERATED_PROMPTS_JSON,
                stage=args.stage,
            )
            if not prompt_records or any(
                record["status"] != "success" for record in prompt_records
            ):
                logger.error(
                    "Post generation failed for one or more URL-derived topics; see %s",
                    config.GENERATED_PROMPTS_JSON,
                )
                return 1
            from image_generator import generate_images

            image_records = generate_images.run(
                input_path=config.GENERATED_PROMPTS_JSON,
                output_path=config.GENERATED_ADS_JSON,
                images_dir=config.GENERATED_ADS_DIR,
            )
            if not image_records or any(
                record["status"] != "success" for record in image_records
            ):
                logger.error(
                    "Image generation failed for one or more URL-derived posts; see %s",
                    config.GENERATED_ADS_JSON,
                )
                return 1
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            logger.error("URL content/image generation failed: %s", exc)
            return 1
    return 0 if trends else 1


if __name__ == "__main__":
    raise SystemExit(main())
