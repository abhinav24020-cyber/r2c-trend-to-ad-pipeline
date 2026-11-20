"""
Refresh the ready ads with the current copy rules and add a video to each.

For every ad in data/output/samples/flux-2-klein-9b-v2: Gemini rewrites the copy and
caption with the topic-specific rules (prompt_generator.ad_creatives), the existing
Klein 9B picture is kept (no new image cost), the poster is laid out again, and a
10-second motion video is rendered. Output: data/output/samples/flux-2-klein-9b-v3/.
Then run scripts/build_web_data.py to publish it on the site.

    python scripts/refresh_showcase.py
"""
import json
import logging
import os
import shutil
import sys

from PIL import Image

sys.path.insert(0, os.getcwd())
from image_generator import compose_ads, video_ads  # noqa: E402
from prompt_generator import ad_creatives  # noqa: E402
from trend_pipeline.gemini_client import get_client  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
SOURCE = os.path.join("data", "output", "samples", "flux-2-klein-9b-v2")
OUT = os.path.join("data", "output", "samples", "flux-2-klein-9b-v3")
TRENDS = os.path.join("data", "output", "scrawl", "trends.json")
COPY_FIELDS = ("audience", "eyebrow", "headline", "subline", "points", "cta", "caption", "hashtags", "fact_used")


def main() -> int:
    report = json.load(open(TRENDS, encoding="utf-8"))
    trends = {t["trend_id"]: t for t in report.get("qualified", [])}
    for t in report.get("near_misses", []):
        trends["near_{}".format(t.get("cluster_id"))] = dict(t, trend_id="near_{}".format(t.get("cluster_id")))
    os.makedirs(os.path.join(OUT, "pictures"), exist_ok=True)
    os.makedirs(os.path.join(OUT, "videos"), exist_ok=True)
    gemini = get_client()
    results = []
    for item in json.load(open(os.path.join(SOURCE, "samples.json"), encoding="utf-8")):
        record, result = item["record"], item.get("result") or {}
        if result.get("status") != "success":
            continue
        trend = trends.get(record["trend_id"])
        post = dict(record.get("post") or {})
        rewritten = False
        if trend is not None:
            fresh = next((r for r in ad_creatives.generate_trend_creatives(trend, gemini)
                          if r["ad_type"] == record["ad_type"] and r["status"] == "success"), None)
            if fresh:
                post.update({k: fresh["post"].get(k) for k in COPY_FIELDS})
                rewritten = True
        picture_path = os.path.join(OUT, "pictures", os.path.basename(result["image_path"]))
        shutil.copyfile(result["image_path"], picture_path)
        ad_path = os.path.join(OUT, os.path.splitext(os.path.basename(result["image_path"]))[0] + "_ad.png")
        compose_ads.compose_poster(picture_path, ad_path, record["aspect_ratio"], post["headline"], post["subline"],
                                   post["cta"], eyebrow=post.get("eyebrow") or "", points=post.get("points") or [],
                                   sticker=post.get("sticker") or "", theme_name=record.get("theme") or "solar")
        ratio = record["aspect_ratio"] if record["aspect_ratio"] in video_ads.VIDEO_SIZES else "9:16"
        video_path = os.path.join(OUT, "videos", record["trend_id"] + ".mp4")
        video_ads.render_video(Image.open(picture_path), post, video_path, record.get("theme") or "solar", ratio)
        new_record = dict(record, post=post)
        results.append({"trend": item["trend"], "record": new_record,
                        "result": dict(result, image_path=os.path.abspath(picture_path),
                                       ad_path=os.path.abspath(ad_path), video_path=os.path.abspath(video_path),
                                       video_format=ratio),
                        "copy_rewritten": rewritten})
        print("{} | copy {} | {}".format(item["trend"][:50], "rewritten" if rewritten else "kept", post["headline"]),
              flush=True)
        json.dump(results, open(os.path.join(OUT, "samples.json"), "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print("DONE: {} ads in {}".format(len(results), OUT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
