"""
The daily automatic run: crawl -> Gate 1/2 -> new ads -> site data.

Run by .github/workflows/daily.yml every morning (07:00 IST), or by hand from the
repository root:

    python scripts/daily_run.py                 # full run
    python scripts/daily_run.py --skip-crawl    # make ads from the last crawl only

Steps:
    1. Crawl every source and rank trends (web_crawler.autopilot.run_cycle). The
       dedup memory (data/output/post_history.json) is kept in the repository, so
       stories seen on earlier days are not picked again.
    2. For each new qualified trend (at most DAILY_MAX_ADS, default 3) make one ad:
       Gemini copy with the topic rules, the creative-director picture idea, two
       FLUX.2 Klein 9B pictures with the better one chosen by Gemini vision, the
       branded poster, and the 10-second video. Formats and colour themes rotate.
       Budget: a Klein 9B picture uses about 1,364 of Cloudflare's 10,000 free daily
       neurons, so 3 ads x 2 pictures (~8,200) is the most that stays free.
    3. Save the ads to web_data/daily.json and their files to static/daily/<id>/.
       New ads start as "pending" until a reviewer approves them on the site
       (decisions live in web_data/reviews.json). Ads older than DAILY_KEEP_DAYS
       (default 7) are removed with their files.
    4. Refresh web_data/site_data.json (latest trends) via scripts/build_web_data.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, os.getcwd())
from trend_pipeline import config  # noqa: E402
from trend_pipeline.web_export import slim_trend  # noqa: E402

logger = logging.getLogger("daily_run")

DAILY_JSON = os.path.join("web_data", "daily.json")
SITE_JSON = os.path.join("web_data", "site_data.json")
DAILY_DIR = os.path.join("static", "daily")
IST = timezone(timedelta(hours=5, minutes=30))
# Format rotation: each new ad takes the next format, so a week's ads cover all placements.
FORMAT_ROTATION = [("4:5", "hook"), ("9:16", "story"), ("1:1", "engagement"), ("1.91:1", "conversion")]
PICTURE_MODEL = "@cf/black-forest-labs/flux-2-klein-9b"
PICTURE_MODEL_NAME = "FLUX.2 Klein 9B"
PICTURE_CANDIDATES = 2
SIMILAR_TITLE = 0.6  # word overlap above which two trend titles count as the same story
_STOP = {"the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "with", "by", "from", "at", "is",
         "its", "new", "ai", "as", "how", "why", "what"}


# --------------------------------------------------------------------------- helpers
def load_json(path: str, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def save_json(path: str, payload: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def title_words(title: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", (title or "").lower()) if w not in _STOP}


def is_same_story(title: str, others: Sequence[str]) -> bool:
    """True when the title shares most of its words with one already made into an ad."""
    words = title_words(title)
    if not words:
        return False
    for other in others:
        theirs = title_words(other)
        if theirs and len(words & theirs) / len(words | theirs) >= SIMILAR_TITLE:
            return True
    return False


def prune_old(entries: List[Dict[str, Any]], keep_days: int, now: datetime) -> List[Dict[str, Any]]:
    """Drop ads older than keep_days and delete their files."""
    cutoff = now - timedelta(days=keep_days)
    kept = []
    for entry in entries:
        try:
            created = datetime.fromisoformat(entry["created_at"])
        except (KeyError, ValueError):
            created = now
        if created >= cutoff:
            kept.append(entry)
            continue
        folder = os.path.join(DAILY_DIR, os.path.basename(str(entry.get("id") or "")))
        if entry.get("id") and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        logger.info("Removed ad older than %d days: %s", keep_days, entry.get("id"))
    return kept


def fit_sticker(text: Optional[str], limit: int = 14) -> str:
    """The corner sticker holds 14 characters: shorten at a word break, never mid-word."""
    text = re.sub(r"\s+", " ", str(text or "")).strip().upper()
    if len(text) <= limit:
        return text
    words = re.split(r"(?<=[ \-])", text)
    out = ""
    for word in words:
        if len((out + word).rstrip(" -")) > limit:
            break
        out += word
    return out.rstrip(" -") or text[:limit]


def pick_trends(trends: Sequence[Dict[str, Any]], made_titles: Sequence[str], limit: int) -> List[Dict[str, Any]]:
    picked: List[Dict[str, Any]] = []
    seen = list(made_titles)
    for trend in trends:
        title = trend.get("topic_title") or ""
        if not title or is_same_story(title, seen):
            continue
        picked.append(trend)
        seen.append(title)
        if len(picked) >= limit:
            break
    return picked


# --------------------------------------------------------------------------- one ad
def make_picture(prompt: str, ratio: str, gemini: Any):
    """Two pictures in parallel, the better one by Gemini vision, polished. Returns (image, scores, chosen)."""
    from image_generator import generate_images, picture_quality

    width, height = config.AD_ASPECT_SIZES[ratio]
    client = generate_images.CloudflareImageClient(
        config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN, PICTURE_MODEL, config.CLOUDFLARE_IMAGE_STEPS)

    def render(_):
        return client.text_to_image(prompt=prompt, width=width, height=height,
                                    negative_prompt=generate_images.NEGATIVE_FOR_BACKGROUND)

    images, failures = [], []
    with ThreadPoolExecutor(max_workers=PICTURE_CANDIDATES) as pool:
        for future in [pool.submit(render, i) for i in range(PICTURE_CANDIDATES)]:
            try:
                images.append(future.result())
            except (RuntimeError, OSError) as exc:
                failures.append(str(exc))
    if not images:
        raise RuntimeError(failures[0] if failures else "no picture was generated")
    best, scores = 0, [None] * len(images)
    if len(images) > 1 and getattr(gemini, "online", False):
        best, scores = picture_quality.choose_best(images, prompt, gemini)
    return picture_quality.polish(images[best]), scores[best], best


def make_ad(trend: Dict[str, Any], ratio: str, ad_type: str, theme: str, gemini: Any,
            now: datetime) -> Optional[Dict[str, Any]]:
    """One finished daily ad (copy, picture, poster, video) or None when the copy fails its checks."""
    from PIL import Image

    from image_generator import compose_ads, video_ads
    from prompt_generator import ad_creatives

    records = ad_creatives.generate_trend_creatives(trend, gemini)
    ok = {r["ad_type"]: r for r in records if r["status"] == "success"}
    # The planned format first; if its copy failed the checks, the next format whose copy passed.
    start = next((i for i, (_, t) in enumerate(FORMAT_ROTATION) if t == ad_type), 0)
    order = FORMAT_ROTATION[start:] + FORMAT_ROTATION[:start]
    choice = next(((r, t) for r, t in order if t in ok), None)
    if choice is None:
        logger.warning("No valid copy in any format for %s; skipped", trend.get("topic_title"))
        return None
    if choice[1] != ad_type:
        logger.info("%s copy failed the checks; using the %s format instead", ad_type, choice[1])
    ratio, ad_type = choice
    record = ok[ad_type]
    record = ad_creatives.reimagine_visual(record, trend, gemini, theme=theme)
    post = dict(record["post"])
    post["sticker"] = fit_sticker(post.get("sticker"))
    prompt = ad_creatives.compose_image_prompt(ad_type, post.get("visual") or {}, theme)
    picture, score, _ = make_picture(prompt, ratio, gemini)

    ad_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(trend.get("trend_id") or now.strftime("ad_%Y%m%d%H%M%S")))
    folder = os.path.join(DAILY_DIR, ad_id)
    os.makedirs(folder, exist_ok=True)
    picture_path = os.path.join(folder, "picture.jpg")
    picture.convert("RGB").save(picture_path, "JPEG", quality=90, optimize=True)

    poster_png = os.path.join(folder, "poster.png")
    compose_ads.compose_poster(picture_path, poster_png, ratio, post["headline"], post.get("subline") or "",
                               post.get("cta") or "Learn More", eyebrow=post.get("eyebrow") or "",
                               points=post.get("points") or [], sticker=fit_sticker(post.get("sticker")),
                               theme_name=theme)
    poster_path = os.path.join(folder, "poster.jpg")
    with Image.open(poster_png) as im:
        im = im.convert("RGB")
        im.thumbnail((1100, 1100))
        im.save(poster_path, "JPEG", quality=85, optimize=True)
    os.remove(poster_png)

    video_ratio = ratio if ratio in video_ads.VIDEO_SIZES else "9:16"
    video_path = os.path.join(folder, "video.mp4")
    video_ads.render_video(Image.open(picture_path), post, video_path, theme, video_ratio)

    def url(path: str) -> str:
        return "/" + path.replace(os.sep, "/")

    return {
        "id": ad_id,
        "date": now.astimezone(IST).strftime("%Y-%m-%d"),
        "created_at": now.isoformat(),
        "source": "daily",
        "trend": slim_trend(trend),
        "ad_type": ad_type, "aspect_ratio": ratio, "placement": record["placement"], "theme": theme,
        "copy": {k: post.get(k) for k in ("audience", "eyebrow", "headline", "subline", "points", "cta",
                                           "caption", "hashtags", "sticker")},
        "concept": (post.get("visual") or {}).get("concept"),
        "image_prompt": prompt,
        "picture": url(picture_path), "poster": url(poster_path),
        "image_model": PICTURE_MODEL_NAME,
        "picture_score": (score or {}).get("overall"),
        "video": url(video_path), "video_format": video_ratio,
    }


# --------------------------------------------------------------------------- run
def run(skip_crawl: bool = False, max_ads: int = 3, keep_days: int = 7) -> Dict[str, Any]:
    from prompt_generator import ad_creatives
    from trend_pipeline.gemini_client import get_client

    started = time.monotonic()
    now = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {"started_at": now.isoformat(), "ads_made": [], "ads_failed": []}

    if not skip_crawl:
        from web_crawler import autopilot, settings
        cycle = autopilot.run_cycle(settings.SEED_QUERIES)
        summary["crawl"] = {"records": cycle.get("records"), "trends": len(cycle.get("trends") or [])}
        logger.info("Crawl done in %.0f min: %s", (time.monotonic() - started) / 60, summary["crawl"])
    trends = load_json(config.RANKED_TRENDS_JSON, [])

    daily = prune_old(load_json(DAILY_JSON, []), keep_days, now)
    showcase = load_json(SITE_JSON, {}).get("showcase", [])
    made = [e["trend"]["topic_title"] for e in daily] + [s["trend"]["topic_title"] for s in showcase]
    todo = pick_trends(trends, made, max_ads)
    summary["new_trends"] = len(todo)
    logger.info("%d qualified trends, %d new ones to turn into ads", len(trends), len(todo))

    gemini = get_client()
    can_make = getattr(gemini, "online", False) and config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN
    if todo and not can_make:
        logger.error("GEMINI_API_KEY / CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN missing: no ads made")
        todo = []
    themes = ad_creatives.THEME_ORDER
    counter = int(now.timestamp() // 86400)  # day number: each day starts the rotation one step further
    for trend in todo:
        ratio, ad_type = FORMAT_ROTATION[counter % len(FORMAT_ROTATION)]
        theme = themes[counter % len(themes)]
        t0 = time.monotonic()
        try:
            entry = make_ad(trend, ratio, ad_type, theme, gemini, now)
        except Exception as exc:  # noqa: BLE001 - one bad ad must not stop the run
            logger.exception("Ad failed for %s", trend.get("topic_title"))
            summary["ads_failed"].append({"trend": trend.get("topic_title"), "error": str(exc)[:300]})
            if "429" in str(exc) or "quota" in str(exc).lower() or "limit" in str(exc).lower():
                logger.error("Daily API limit reached; stopping for today")
                break
            continue
        counter += 1
        if entry is None:
            summary["ads_failed"].append({"trend": trend.get("topic_title"), "error": "copy failed the checks"})
            continue
        daily = [e for e in daily if e.get("id") != entry["id"]]  # a re-run replaces, never duplicates
        daily.insert(0, entry)
        save_json(DAILY_JSON, daily)  # save after every ad, so a later failure keeps the earlier ones
        summary["ads_made"].append({"id": entry["id"], "headline": entry["copy"]["headline"],
                                    "format": ratio, "theme": theme,
                                    "seconds": round(time.monotonic() - t0)})
        logger.info("Ad %d/%d done in %.0fs: %s", len(summary["ads_made"]), len(todo),
                    time.monotonic() - t0, entry["copy"]["headline"])
    save_json(DAILY_JSON, daily)

    # Latest trends on the site.
    if os.path.isdir(os.path.join("data", "output", "samples")):
        from scripts import build_web_data
        build_web_data.main()

    summary["minutes"] = round((time.monotonic() - started) / 60, 1)
    summary["ads_on_site"] = len(daily)
    return summary


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily run: crawl, rank, make new ads, update the site data.")
    parser.add_argument("--skip-crawl", action="store_true", help="Use the last crawl's ranked trends")
    parser.add_argument("--max-ads", type=int, default=int(os.environ.get("DAILY_MAX_ADS", "3")))
    parser.add_argument("--keep-days", type=int, default=int(os.environ.get("DAILY_KEEP_DAYS", "7")))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    logging.getLogger("crawlee").setLevel(logging.CRITICAL)
    summary = run(skip_crawl=args.skip_crawl, max_ads=args.max_ads, keep_days=args.keep_days)
    print("DAILY RUN SUMMARY")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:  # shows on the run's page in GitHub Actions
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write("## Daily run\n\n- Minutes: {}\n- New trends: {}\n- Ads made: {}\n- Ads failed: {}\n- Ads on site: {}\n\n"
                         .format(summary["minutes"], summary.get("new_trends"), len(summary["ads_made"]),
                                 len(summary["ads_failed"]), summary["ads_on_site"]))
            for ad in summary["ads_made"]:
                handle.write("- {} ({}, {})\n".format(ad["headline"], ad["format"], ad["theme"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
