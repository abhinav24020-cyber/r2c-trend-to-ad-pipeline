"""
Package pipeline output for the web app (app.py), so it runs without data/ (e.g. on Vercel).

Writes web_data/site_data.json (latest trends, the ad showcase, the model benchmark) and
static/showcase/{pictures,posters}/*.jpg. Run from the repository root after a new crawl
or a new sample run:

    python scripts/build_web_data.py
"""
import json
import os
import shutil
import sys

from PIL import Image

sys.path.insert(0, os.getcwd())
from prompt_generator import ad_creatives  # noqa: E402
from trend_pipeline import config  # noqa: E402
from trend_pipeline.web_export import slim_trend  # noqa: E402

SAMPLES = os.path.join("data", "output", "samples")
# The newest showcase run: v3 has the topic-specific copy and a video per ad (scripts/refresh_showcase.py).
SHOWCASE_RUN = next(p for p in (os.path.join(SAMPLES, "flux-2-klein-9b-v3"), os.path.join(SAMPLES, "flux-2-klein-9b-v2"))
                    if os.path.isfile(os.path.join(p, "samples.json")))
SHOWCASE_TRENDS = os.path.join("data", "output", "scrawl", "trends.json")
OUT_JSON = os.path.join("web_data", "site_data.json")
OUT_IMG = os.path.join("static", "showcase")
MODELS = [  # folder, name, size, where
    ("flux-2-klein-9b-v2", "FLUX.2 Klein 9B", "9B", "Cloudflare Workers AI"),
    ("20261029_1352", "FLUX.2 Klein 4B", "4B", "Cloudflare Workers AI"),
    ("stable-diffusion-3.5-medium", "SD 3.5 Medium", "2.5B", "Hugging Face Space"),
    ("stable-diffusion-xl-base-1.0", "SDXL base 1.0", "~3.5B", "Cloudflare Workers AI"),
]
CRITERIA = ("adherence", "realism", "catchiness", "artifacts")


def repo_path(path):
    """A file path recorded on another machine (a Windows path such as C:/Users/.../data/output/x.png) as a path
    inside this repository ("data/output/x.png"), so the build works on Windows and on Linux CI."""
    if not path:
        return None
    if os.path.isfile(path):
        return path
    norm = path.replace("\\", "/")
    for anchor in ("data/output/", "static/"):
        at = norm.find(anchor)
        if at >= 0:
            candidate = os.path.join(*norm[at:].split("/"))
            if os.path.isfile(candidate):
                return candidate
    return None


def published(dest):
    return "/" + dest.replace(os.sep, "/")


def copy_video(src, trend_id):
    dest = os.path.join(OUT_IMG, "videos", trend_id + ".mp4")
    src = repo_path(src)
    if not src:  # source not in this checkout: keep the copy already published, if any
        return published(dest) if os.path.isfile(dest) else None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.abspath(src) != os.path.abspath(dest):
        shutil.copyfile(src, dest)
    return published(dest)


def save_jpg(src, dest, max_side=1344, quality=88):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = repo_path(src)
    if not src:  # source not in this checkout: keep the copy already published
        if os.path.isfile(dest):
            return published(dest)
        raise FileNotFoundError("picture missing and never published: " + dest)
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        im.save(dest, "JPEG", quality=quality, optimize=True)
    return published(dest)


def main():
    # Latest crawl: qualified trends + near misses.
    latest = [slim_trend(t) for t in json.load(open(config.RANKED_TRENDS_JSON, encoding="utf-8"))]
    if os.path.isfile(config.DROPPED_TRENDS_JSON):
        latest += [slim_trend(t, near_miss=True)
                   for t in json.load(open(config.DROPPED_TRENDS_JSON, encoding="utf-8")) if t.get("near_miss")]

    # Ad showcase: the approved v2 ads (copy, picture, poster, prompt) and their trends.
    report = json.load(open(SHOWCASE_TRENDS, encoding="utf-8"))
    by_id = {}
    for t in report.get("qualified", []):
        by_id[t["trend_id"]] = slim_trend(t)
    for t in report.get("near_misses", []):
        st = slim_trend(t, near_miss=True)
        by_id[st["trend_id"]] = st
    showcase = []
    for item in json.load(open(os.path.join(SHOWCASE_RUN, "samples.json"), encoding="utf-8")):
        rec, res = item["record"], item.get("result") or {}
        if res.get("status") != "success":
            continue
        post = rec.get("post") or {}
        tid = rec["trend_id"]
        showcase.append({
            "trend": by_id.get(tid) or {"trend_id": tid, "topic_title": item["trend"]},
            "ad_type": rec["ad_type"], "aspect_ratio": rec["aspect_ratio"], "placement": rec["placement"],
            "theme": rec.get("theme") or "solar",
            "copy": {k: post.get(k) for k in ("audience", "eyebrow", "headline", "subline", "points", "cta",
                                               "caption", "hashtags", "sticker")},
            "concept": (post.get("visual") or {}).get("concept"),
            "image_prompt": rec.get("image_prompt"),
            "picture": save_jpg(res["image_path"], os.path.join(OUT_IMG, "pictures", tid + ".jpg")),
            "poster": save_jpg(res["ad_path"], os.path.join(OUT_IMG, "posters", tid + ".jpg"), 1100, 85),
            "image_model": "FLUX.2 Klein 9B",
            "video": copy_video(res.get("video_path"), tid),
            "video_format": res.get("video_format"),
        })

    # Model benchmark.
    scores = json.load(open(os.path.join(SAMPLES, "model_scores.json"), encoding="utf-8"))
    bench = []
    for folder, name, size, where in MODELS:
        vals = [v for k, v in scores.items() if k.startswith(folder + "|")]
        if not vals:
            continue
        avg = {c: round(sum(float(v[c]) for v in vals) / len(vals), 1) for c in CRITERIA}
        avg["overall"] = round(sum(sum(float(v[c]) for c in CRITERIA) / 4 for v in vals) / len(vals), 1)
        bench.append({"model": name, "size": size, "where": where, "images": len(vals), **avg})
    bench.sort(key=lambda b: -b["overall"])

    data = {
        "latest_trends": latest,
        "latest_generated_at": max((t.get("generated_at") or "" for t in latest), default=""),
        "showcase": showcase,
        "benchmark": bench,
        "benchmark_not_tested": [{"model": "Qwen-Image-2512", "size": "20B",
                                  "reason": "Needs paid GPU time (free Hugging Face quota is too small)."}],
        "formats": {"4:5": "Instagram / Facebook feed", "9:16": "Stories / Reels",
                    "1:1": "LinkedIn / Facebook feed", "1.91:1": "LinkedIn / Facebook link ad"},
        "themes": ad_creatives.THEMES,
        "ctas": ad_creatives.ALLOWED_CTAS,
        "benefits": ad_creatives.BENEFITS,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    json.dump(data, open(OUT_JSON, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("latest trends: {} · showcase ads: {} · benchmark models: {}".format(len(latest), len(showcase), len(bench)))


if __name__ == "__main__":
    main()
