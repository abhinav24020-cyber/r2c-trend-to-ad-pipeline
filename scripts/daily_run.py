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
QUEUE_JSON = os.path.join("web_data", "video_queue.json")
REVIEWS_JSON = os.path.join("web_data", "reviews.json")
LAST_RUN_JSON = os.path.join("web_data", "last_run.json")
# Trend videos (the approved sample's process) for every new ad; DAILY_TREND_VIDEOS=0 turns them off.
TREND_VIDEOS = os.environ.get("DAILY_TREND_VIDEOS", "1") != "0"
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


def prune_old(entries: List[Dict[str, Any]], keep_days: int, now: datetime,
              approved: Sequence[str] = ()) -> List[Dict[str, Any]]:
    """Drop ads older than keep_days and delete their files. Approved ads are kept: they build the gallery."""
    cutoff = now - timedelta(days=keep_days)
    kept = []
    for entry in entries:
        if entry.get("id") in approved:
            kept.append(entry)
            continue
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
    if limit <= 0:  # --max-ads 0: crawl and rank only
        return picked
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


def ad_id_for(trend: Dict[str, Any], now: datetime) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(trend.get("trend_id") or now.strftime("ad_%Y%m%d%H%M%S")))


def crop_to(picture: Any, ratio: str) -> Any:
    """Centre crop of a picture to an ad format's size (the video's 9:16 picture reused for the ad photo)."""
    from PIL import Image

    width, height = config.AD_ASPECT_SIZES[ratio]
    pw, ph = picture.size
    scale = max(width / pw, height / ph)
    resized = picture.resize((round(pw * scale), round(ph * scale)), Image.LANCZOS)
    left, top = (resized.width - width) // 2, max(0, (resized.height - height) // 3)  # keep the upper-middle (faces)
    return resized.crop((left, top, left + width, top + height))


def make_ad(trend: Dict[str, Any], ratio: str, ad_type: str, theme: str, gemini: Any,
            now: datetime, picture_path: Optional[str] = None, simple_video: bool = True) -> Optional[Dict[str, Any]]:
    """One finished daily ad (copy, picture, poster, video) or None when the copy fails its checks.
    picture_path: use this picture (the trend video's main scene) instead of generating new ones."""
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
    if picture_path:  # the video's FLUX.2 Klein 9B picture: no extra pictures from the daily allowance
        if ratio == "1.91:1":  # a vertical picture cannot fill a wide banner: square post instead
            ratio = "1:1"
        with Image.open(picture_path) as source:
            picture, score = crop_to(source.convert("RGB"), ratio), None
        prompt = "(the trend video's main scene picture)"
    else:
        picture, score, _ = make_picture(prompt, ratio, gemini)

    ad_id = ad_id_for(trend, now)
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
    if simple_video:
        video_ads.render_video(Image.open(picture_path), post, video_path, theme, video_ratio)

    def url(path: str) -> str:
        return "/" + path.replace(os.sep, "/")

    return {
        "id": ad_id,
        "date": now.astimezone(IST).strftime("%Y-%m-%d"),
        "created_at": now.isoformat(),
        "source": "daily",
        "trend": trend if "composite_trend_score" not in trend else slim_trend(trend),
        "ad_type": ad_type, "aspect_ratio": ratio, "placement": record["placement"], "theme": theme,
        "copy": {k: post.get(k) for k in ("audience", "eyebrow", "headline", "subline", "points", "cta",
                                           "caption", "hashtags", "sticker")},
        "concept": (post.get("visual") or {}).get("concept"),
        "image_prompt": prompt,
        "picture": url(picture_path), "poster": url(poster_path),
        "image_model": PICTURE_MODEL_NAME,
        "picture_score": (score or {}).get("overall"),
        "video": url(video_path) if simple_video else None, "video_format": video_ratio,
    }


# --------------------------------------------------------------------------- ad + trend video
def video_caption(trend: Dict[str, Any], script: Dict[str, Any], gemini: Any) -> Dict[str, Any]:
    """Post caption + hashtags for the ad, written from the video script and the trend's facts. gpt-oss-120b on
    Cloudflare first, Gemini as backup; it must name at least two of the trend's own terms, otherwise the voiceover
    lines are used as the caption."""
    from image_generator import trend_video

    terms = sorted(trend_video.trend_terms(trend))
    scenes = script["scenes"]
    prompt = (
        "Write the social media post caption for this video ad (Instagram, LinkedIn).\n"
        "TREND: {}\nKEY FACTS (only these may be stated): {}\nVIDEO SCENES: {}\nCTA: {}\n"
        "Rules: 40-70 words, plain language, 2-3 sentences. Name at least two of these trend terms exactly as written: "
        "{}. Say why it matters for researchers in this area, then that R2C.Ai at IIIT Delhi connects them with industry "
        "partners, and end with the CTA. No hype words, no invented numbers, no emojis. Then 5 hashtags without spaces, "
        "specific to this trend plus #R2CAi. Return JSON: {{\"caption\": str, \"hashtags\": [str]}}"
    ).format(trend.get("topic_title"), json.dumps(trend.get("topic_key_facts") or [], ensure_ascii=False),
             json.dumps([s.get("voiceover") for s in scenes], ensure_ascii=False), script.get("cta"), ", ".join(terms[:20]))
    writers = [("gpt-oss-120b", lambda: trend_video._cloudflare_text(prompt))]
    if getattr(gemini, "online", False):
        writers.append(("gemini", lambda: gemini.generate_text(prompt, label="video_caption", json_mode=True)))
    for name, write in writers:
        try:
            data = trend_video._parse_json(write() or "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Caption by %s failed: %s", name, str(exc)[:160])
            continue
        caption = str(data.get("caption") or "").strip()
        tags = ["#" + str(t).lstrip("#").replace(" ", "") for t in (data.get("hashtags") or [])][:6]
        if sum(1 for t in terms if t in caption) >= 2 and 25 <= len(caption.split()) <= 90:
            return {"caption": caption, "hashtags": tags, "caption_writer": name}
        logger.info("Caption by %s missed the trend terms or length; trying the next writer", name)
    return {"caption": " ".join(s["voiceover"] for s in scenes), "hashtags": ["#R2CAi", "#AIResearch"],
            "caption_writer": "video voiceover"}


def entry_from_video(trend: Dict[str, Any], video: Dict[str, Any], theme: str, now: datetime,
                     ratio: str = "9:16", ad_type: str = "story", gemini: Any = None) -> Dict[str, Any]:
    """The ad photo + poster made from the video: its main picture cropped to the format, the headline and subline
    from the trend-checked video script, the R2C.Ai points from its chips, and a trend-specific post caption."""
    from PIL import Image

    from image_generator import compose_ads

    if ratio == "1.91:1":  # a vertical picture cannot fill a wide banner: square post instead
        ratio = "1:1"
    script = video["script"]
    scenes = script["scenes"]
    ad_id = ad_id_for(trend, now)
    folder = os.path.join(DAILY_DIR, ad_id)
    os.makedirs(folder, exist_ok=True)
    picture_path = os.path.join(folder, "picture.jpg")
    with Image.open(video["picture"]) as source:
        crop_to(source.convert("RGB"), ratio).save(picture_path, "JPEG", quality=90, optimize=True)
    caption = video_caption(trend, script, gemini)
    copy = {"audience": script.get("audience_line"), "eyebrow": script.get("tag"), "headline": scenes[0]["on_screen"],
            "subline": scenes[1]["voiceover"], "points": list(script.get("chips") or [])[:3], "cta": script.get("cta"),
            "caption": caption["caption"], "hashtags": caption["hashtags"], "sticker": ""}
    poster_png = os.path.join(folder, "poster.png")
    compose_ads.compose_poster(picture_path, poster_png, ratio, copy["headline"], copy["subline"], copy["cta"] or "Learn More",
                               eyebrow=copy["eyebrow"] or "", points=copy["points"], sticker="", theme_name=theme)
    poster_path = os.path.join(folder, "poster.jpg")
    with Image.open(poster_png) as im:
        im = im.convert("RGB")
        im.thumbnail((1100, 1100))
        im.save(poster_path, "JPEG", quality=85, optimize=True)
    os.remove(poster_png)
    placement = {"9:16": "story", "4:5": "feed", "1:1": "feed"}.get(ratio, "feed")
    return {"id": ad_id, "date": now.astimezone(IST).strftime("%Y-%m-%d"), "created_at": now.isoformat(),
            "source": "daily", "trend": trend if "composite_trend_score" not in trend else slim_trend(trend),
            "ad_type": ad_type, "aspect_ratio": ratio, "placement": placement, "theme": theme, "copy": copy,
            "caption_writer": caption["caption_writer"], "concept": None, "image_prompt": scenes[0].get("image_prompt"),
            "picture": "/" + picture_path.replace(os.sep, "/"), "poster": "/" + poster_path.replace(os.sep, "/"),
            "image_model": PICTURE_MODEL_NAME, "picture_score": None, "video": None, "video_format": "9:16"}


def make_ad_with_video(trend: Dict[str, Any], ratio: str, ad_type: str, theme: str, gemini: Any,
                       now: datetime) -> Optional[Dict[str, Any]]:
    """The trend-specific video (image_generator.trend_video), then the ad photo, poster and caption made from it
    (same picture, same trend-checked words). A failed video raises: the trend is retried another day."""
    from image_generator import trend_video

    folder = os.path.join(DAILY_DIR, ad_id_for(trend, now))
    video = trend_video.make_trend_video(trend, folder, gemini)
    entry = entry_from_video(trend, video, theme, now, ratio, ad_type, gemini)
    return add_video_fields(entry, video)


def add_video_fields(entry: Dict[str, Any], video: Dict[str, Any]) -> Dict[str, Any]:
    script = video["script"]
    entry.update({
        "video": "/" + video["video"].replace(os.sep, "/"), "video_format": "9:16", "video_kind": "trend",
        "video_seconds": video["duration"], "video_motion_clips": video["motion_clips"],
        "video_writer": script.get("writer"), "video_check": script.get("check"),
        "video_script": [{"role": s.get("role"), "on_screen": s.get("on_screen"), "voiceover": s.get("voiceover"),
                          "fact_used": s.get("fact_used")} for s in script["scenes"]],
    })
    return entry


def load_queue() -> List[Dict[str, Any]]:
    """Trends waiting for a video (web_data/video_queue.json), served before the day's new trends."""
    return [t for t in load_json(QUEUE_JSON, []) if isinstance(t, dict) and t.get("topic_title")]


# --------------------------------------------------------------------------- run
def ran_today(now: datetime) -> bool:
    """True when a full daily run already finished today (IST): the backup schedule then does nothing."""
    last = load_json(LAST_RUN_JSON, {})
    try:
        finished = datetime.fromisoformat(last["finished_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return not last.get("skipped_crawl") and finished.astimezone(IST).date() == now.astimezone(IST).date()


def run(skip_crawl: bool = False, max_ads: int = 2, keep_days: int = 7) -> Dict[str, Any]:
    from prompt_generator import ad_creatives
    from trend_pipeline.gemini_client import get_client

    started = time.monotonic()
    now = datetime.now(timezone.utc)
    summary: Dict[str, Any] = {"started_at": now.isoformat(), "skipped_crawl": skip_crawl,
                               "trend_videos": TREND_VIDEOS, "ads_made": [], "ads_failed": []}

    if not skip_crawl:
        from web_crawler import autopilot, settings
        cycle = autopilot.run_cycle(settings.SEED_QUERIES)
        summary["crawl"] = {"records": cycle.get("records"), "trends": len(cycle.get("trends") or [])}
        logger.info("Crawl done in %.0f min: %s", (time.monotonic() - started) / 60, summary["crawl"])
    trends = load_json(config.RANKED_TRENDS_JSON, [])

    reviews = load_json(REVIEWS_JSON, {})
    approved = [ad_id for ad_id, r in reviews.items() if isinstance(r, dict) and r.get("status") == "approved"]
    daily = prune_old(load_json(DAILY_JSON, []), keep_days, now, approved)
    showcase = load_json(SITE_JSON, {}).get("showcase", [])
    made = [e["trend"]["topic_title"] for e in daily] + [s["trend"]["topic_title"] for s in showcase]
    # Trends waiting from earlier days come first, then today's new ones.
    queue = [t for t in load_queue() if not is_same_story(t["topic_title"], made)]
    todo = queue[:max_ads]
    todo += pick_trends(trends, made + [t["topic_title"] for t in queue], max_ads - len(todo))
    summary["from_queue"] = min(len(queue), max_ads)
    summary["new_trends"] = len(todo) - summary["from_queue"]
    logger.info("%d qualified trends; %d queued + %d new to turn into ads", len(trends), summary["from_queue"],
                summary["new_trends"])

    gemini = get_client()
    can_make = config.CLOUDFLARE_ACCOUNTS and (
        TREND_VIDEOS or getattr(gemini, "online", False))
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
            if TREND_VIDEOS:
                entry = make_ad_with_video(trend, ratio, ad_type, theme, gemini, now)
            else:
                entry = make_ad(trend, ratio, ad_type, theme, gemini, now)
        except Exception as exc:  # noqa: BLE001 - one bad ad must not stop the run
            logger.exception("Ad failed for %s", trend.get("topic_title"))
            summary["ads_failed"].append({"trend": trend.get("topic_title"), "error": str(exc)[:300]})
            text = str(exc).lower()
            if "429" in text or "quota" in text or "limit" in text or "allocation" in text:
                # keep today's unfinished trends for tomorrow, when the free allowance is back
                waiting = todo[todo.index(trend):]
                queue += [t if "composite_trend_score" not in t else slim_trend(t) for t in waiting
                          if not is_same_story(t.get("topic_title") or "", [q["topic_title"] for q in queue])]
                save_json(QUEUE_JSON, queue)
                logger.error("Daily API limit reached; %d trend(s) queued for tomorrow", len(waiting))
                break
            continue
        counter += 1
        if entry is None:
            summary["ads_failed"].append({"trend": trend.get("topic_title"), "error": "copy failed the checks"})
            continue
        daily = [e for e in daily if e.get("id") != entry["id"]]  # a re-run replaces, never duplicates
        daily.insert(0, entry)
        save_json(DAILY_JSON, daily)  # save after every ad, so a later failure keeps the earlier ones
        queue = [t for t in queue if t.get("topic_title") != trend.get("topic_title")]
        save_json(QUEUE_JSON, queue)
        summary["ads_made"].append({"id": entry["id"], "headline": entry["copy"]["headline"],
                                    "format": entry.get("aspect_ratio"), "theme": theme,
                                    "video": entry.get("video_kind") or ("simple" if entry.get("video") else None),
                                    "video_seconds": entry.get("video_seconds"),
                                    "motion_clips": entry.get("video_motion_clips"),
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
    summary["queue_left"] = len(queue)
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    save_json(LAST_RUN_JSON, summary)  # readable on GitHub without opening the run log
    return summary


def default_max_ads() -> int:
    """DAILY_MAX_ADS if set to a number, otherwise 2 trend videos per configured Cloudflare account."""
    value = os.environ.get("DAILY_MAX_ADS", "").strip()
    return int(value) if value.isdigit() else 2 * max(1, len(config.CLOUDFLARE_ACCOUNTS))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Daily run: crawl, rank, make new ads + trend videos, update the site data.")
    parser.add_argument("--skip-crawl", action="store_true", help="Use the last crawl's ranked trends")
    parser.add_argument("--max-ads", type=int, default=default_max_ads(),
                        help="Default: 2 per Cloudflare account (what fits the free daily allowance)")
    parser.add_argument("--keep-days", type=int, default=int(os.environ.get("DAILY_KEEP_DAYS", "7")))
    parser.add_argument("--if-not-ran-today", action="store_true",
                        help="Do nothing if a full run already finished today (IST); used by the backup schedule")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stdout)
    logging.getLogger("crawlee").setLevel(logging.CRITICAL)
    if args.if_not_ran_today and ran_today(datetime.now(timezone.utc)):
        print("A full daily run already finished today; nothing to do.")
        return 0
    summary = run(skip_crawl=args.skip_crawl, max_ads=args.max_ads, keep_days=args.keep_days)
    print("DAILY RUN SUMMARY")
    print(json.dumps(summary, indent=1, ensure_ascii=False))
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:  # shows on the run's page in GitHub Actions
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write("## Daily run\n\n- Minutes: {}\n- From the video queue: {}\n- New trends: {}\n- Ads made: {}\n"
                         "- Ads failed: {}\n- Ads on site: {}\n- Still queued: {}\n\n"
                         .format(summary["minutes"], summary.get("from_queue"), summary.get("new_trends"),
                                 len(summary["ads_made"]), len(summary["ads_failed"]), summary["ads_on_site"],
                                 summary["queue_left"]))
            for ad in summary["ads_made"]:
                handle.write("- {} ({}, {}, video: {})\n".format(ad["headline"], ad["format"], ad["theme"], ad["video"]))
            for bad in summary["ads_failed"]:
                handle.write("- FAILED {}: {}\n".format(bad["trend"], bad["error"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
