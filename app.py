"""
R2C.Ai Ad Studio: the web app for the trend-to-ad pipeline (local and Vercel).

Pages and API:
    GET  /                      the studio (templates/index.html)
    GET  /api/site-data         latest trends, the ad showcase and the model benchmark (web_data/site_data.json),
                                plus the daily run's ads with their review status (web_data/daily.json)
    POST /api/review            approve or reject a daily ad (reviewer passcode R2C_REVIEW_KEY)
    POST /api/compose           picture + ad copy -> finished R2C poster ad for a format and colour theme
    POST /api/generate-picture  image prompt -> new text-free picture (Cloudflare Workers AI, FLUX.2 Klein)
    POST /api/scrape            URLs -> scraped, Gate 1/2-ranked trends
    POST /api/brief             trend -> R2C ad copy and image prompt (Gemini, prompt_generator.ad_creatives)
    POST /api/generate-ad       legacy: prompt -> picture saved under GENERATED_ADS_DIR

Images travel as base64 data URLs instead of files: on a serverless host each request may
run on a different instance, and only /tmp is writable.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Tuple

from flask import Flask, jsonify, render_template, request, send_from_directory

from trend_pipeline import config

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE_DATA = os.path.join(ROOT, "web_data", "site_data.json")
SHOWCASE_DIR = os.path.join(ROOT, "static", "showcase")
DAILY_JSON = os.path.join(ROOT, "web_data", "daily.json")      # ads from the daily run (scripts/daily_run.py)
REVIEWS_JSON = os.path.join(ROOT, "web_data", "reviews.json")  # reviewer decisions: {ad id: approved|rejected}
DAILY_DIR = os.path.join(ROOT, "static", "daily")
REVIEW_DECISIONS = ("approved", "rejected", "pending")

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024  # a picture travels as a data URL
app.json.sort_keys = False  # keep formats and themes in their intended order
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("r2c_web_app")
os.makedirs(config.DATA_DIR, exist_ok=True)
os.makedirs(config.GENERATED_ADS_DIR, exist_ok=True)

# Format chosen in the UI -> poster aspect ratio and ad type (prompt_generator.ad_creatives.AD_FORMATS).
FORMAT_AD_TYPE = {"4:5": "hook", "9:16": "story", "1:1": "engagement", "1.91:1": "conversion"}
IMAGE_MODELS = {
    "klein4b": "@cf/black-forest-labs/flux-2-klein-4b",
    "klein9b": "@cf/black-forest-labs/flux-2-klein-9b",
}
# Legacy format names used by /api/generate-ad.
FORMAT_ASPECT_RATIO = {"X (Twitter)": "16:9", "LinkedIn": "1:1", "Instagram": "4:5", "Facebook": "4:5"}


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _data_url(image, fmt: str = "JPEG", quality: int = 90) -> str:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format=fmt, quality=quality)
    return "data:image/{};base64,{}".format(fmt.lower(), base64.b64encode(buffer.getvalue()).decode("ascii"))


def _load_picture(source: str):
    """A picture from the bundled showcase (/static/showcase/...) or a data URL sent by the browser."""
    from PIL import Image

    if source.startswith("data:image/"):
        try:
            raw = base64.b64decode(source.split(",", 1)[1], validate=True)
        except (ValueError, IndexError) as exc:
            raise ValueError("The picture data is not valid base64.") from exc
        return Image.open(io.BytesIO(raw))
    for prefix, folder in (("/static/showcase/", SHOWCASE_DIR), ("/static/daily/", DAILY_DIR)):
        if source.startswith(prefix):
            path = os.path.normpath(os.path.join(ROOT, source.lstrip("/")))
            if path.startswith(folder + os.sep) and os.path.isfile(path):
                return Image.open(path)
    raise ValueError("Unknown picture.")


def _ratio_size(ratio: str) -> Tuple[int, int]:
    if ratio not in config.AD_ASPECT_SIZES:
        raise ValueError("Unknown format {!r}.".format(ratio))
    return config.AD_ASPECT_SIZES[ratio]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(os.path.join(ROOT, "static", "img"), "favicon.png", mimetype="image/png")


@app.route("/api/site-data", methods=["GET"])
def site_data():
    with open(SITE_DATA, encoding="utf-8") as handle:
        data = json.load(handle)
    reviews = _read_json(REVIEWS_JSON, {})
    daily = []
    for entry in _read_json(DAILY_JSON, []):
        status = reviews.get(entry.get("id"), {}).get("status", "pending")
        daily.append(dict(entry, status=status if status in REVIEW_DECISIONS else "pending"))
    data["daily"] = daily
    data["review_enabled"] = bool(os.environ.get("R2C_REVIEW_KEY"))
    return jsonify(data)


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _github_repo() -> Tuple[str, str]:
    """(owner/name, branch) of the repository Vercel deploys from; GITHUB_REPO / GITHUB_BRANCH override."""
    repo = os.environ.get("GITHUB_REPO") or "{}/{}".format(
        os.environ.get("VERCEL_GIT_REPO_OWNER", ""), os.environ.get("VERCEL_GIT_REPO_SLUG", ""))
    branch = os.environ.get("GITHUB_BRANCH") or os.environ.get("VERCEL_GIT_COMMIT_REF") or "main"
    if repo.strip("/") != repo or "/" not in repo:
        raise RuntimeError("GITHUB_REPO is not set.")
    return repo, branch


@app.route("/api/cron/daily", methods=["GET"])
def cron_daily():
    """
    Vercel cron (vercel.json, 07:00 IST): start the daily GitHub Actions run now. GitHub's own schedule often
    starts hours late; a workflow_dispatch starts within a minute. Vercel sends "Authorization: Bearer
    <CRON_SECRET>"; the call needs GITHUB_TOKEN with Actions: write on the repository.
    """
    import hmac
    import urllib.error
    import urllib.request

    secret = os.environ.get("CRON_SECRET", "")
    sent = request.headers.get("Authorization", "")
    if not secret or not hmac.compare_digest(sent.encode("utf-8"), ("Bearer " + secret).encode("utf-8")):
        return _error("Not allowed.", 401)
    if not os.environ.get("GITHUB_TOKEN"):
        return _error("GITHUB_TOKEN is not set.", 503)
    try:
        repo, branch = _github_repo()
    except RuntimeError as exc:
        return _error(str(exc), 503)
    url = "https://api.github.com/repos/{}/actions/workflows/daily.yml/dispatches".format(repo)
    req = urllib.request.Request(url, data=json.dumps({"ref": branch}).encode("utf-8"), method="POST", headers={
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json",
        "User-Agent": "r2c-ad-studio", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).close()
    except urllib.error.HTTPError as exc:
        logger.error("Starting the daily run failed: %s", exc.code)
        hint = " (the token needs Actions: Read and write)" if exc.code in (403, 404) else ""
        return _error("GitHub refused to start the daily run ({}){}".format(exc.code, hint), 502)
    except OSError as exc:
        return _error("Could not reach GitHub: {}".format(exc), 502)
    return jsonify({"started": "daily run", "repo": repo, "branch": branch})


def _github_save_review(ad_id: str, decision: str, reviewer: str) -> None:
    """
    Record a decision in web_data/reviews.json in the GitHub repository Vercel deploys from,
    so it survives redeploys; Vercel then redeploys the site with it (about a minute).
    Needs GITHUB_TOKEN (contents: write on that repository). The repository defaults to the
    one Vercel built from (VERCEL_GIT_REPO_OWNER / VERCEL_GIT_REPO_SLUG / VERCEL_GIT_COMMIT_REF).
    """
    import urllib.error
    import urllib.request

    repo, branch = _github_repo()
    url = "https://api.github.com/repos/{}/contents/web_data/reviews.json".format(repo)
    headers = {"Authorization": "Bearer " + os.environ["GITHUB_TOKEN"], "Accept": "application/vnd.github+json",
               "User-Agent": "r2c-ad-studio"}
    for _ in range(3):  # retry when another decision was saved in between (sha conflict)
        reviews, sha = {}, None
        try:
            with urllib.request.urlopen(urllib.request.Request(url + "?ref=" + branch, headers=headers),
                                        timeout=20) as response:
                current = json.load(response)
            sha = current["sha"]
            reviews = json.loads(base64.b64decode(current["content"]).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise RuntimeError("GitHub read failed ({})".format(exc.code)) from exc
        reviews[ad_id] = {"status": decision, "by": reviewer,
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        body = {"message": "Review: {} {}".format(decision, ad_id), "branch": branch,
                "content": base64.b64encode((json.dumps(reviews, indent=1, ensure_ascii=False) + "\n")
                                            .encode("utf-8")).decode("ascii")}
        if sha:
            body["sha"] = sha
        request_put = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="PUT",
                                             headers=dict(headers, **{"Content-Type": "application/json"}))
        try:
            urllib.request.urlopen(request_put, timeout=20).close()
            return
        except urllib.error.HTTPError as exc:
            if exc.code not in (409, 422):
                raise RuntimeError("GitHub write failed ({})".format(exc.code)) from exc
    raise RuntimeError("GitHub write kept conflicting; try again.")


@app.route("/api/review", methods=["POST"])
def review():
    """Approve or reject a daily ad. Protected by the reviewer passcode R2C_REVIEW_KEY."""
    import hmac

    body = request.get_json(silent=True) or {}
    ad_id = str(body.get("id") or "")
    decision = str(body.get("decision") or "")
    reviewer = str(body.get("reviewer") or "reviewer").strip()[:40] or "reviewer"
    expected = os.environ.get("R2C_REVIEW_KEY", "")
    if not expected:
        return _error("Reviewing is not set up: add R2C_REVIEW_KEY to the server's environment.", 503)
    if not hmac.compare_digest(str(body.get("key") or "").encode("utf-8"), expected.encode("utf-8")):
        return _error("Wrong reviewer passcode.", 403)
    if decision not in REVIEW_DECISIONS:
        return _error("decision must be approved, rejected or pending.")
    if not any(e.get("id") == ad_id for e in _read_json(DAILY_JSON, [])):
        return _error("Unknown ad.", 404)
    if os.environ.get("GITHUB_TOKEN"):
        try:
            _github_save_review(ad_id, decision, reviewer)
        except (RuntimeError, OSError) as exc:
            logger.error("Saving the review failed: %s", exc)
            return _error("Could not save the decision: {}".format(exc), 502)
        return jsonify({"id": ad_id, "status": decision, "saved": "github"})
    if os.environ.get("VERCEL"):
        return _error("Reviewing on Vercel needs GITHUB_TOKEN so decisions are saved to the repository.", 503)
    reviews = _read_json(REVIEWS_JSON, {})  # local run: write the file directly
    reviews[ad_id] = {"status": decision, "by": reviewer,
                      "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with open(REVIEWS_JSON, "w", encoding="utf-8") as handle:
        json.dump(reviews, handle, indent=1, ensure_ascii=False)
        handle.write("\n")
    return jsonify({"id": ad_id, "status": decision, "saved": "local"})


@app.route("/api/compose", methods=["POST"])
def compose():
    """Lay out the R2C poster: picture (cover-cropped to the format) + copy + colour theme."""
    from image_generator import compose_ads
    from prompt_generator import ad_creatives

    body = request.get_json(silent=True) or {}
    copy = body.get("copy") or {}
    ratio = str(body.get("format") or "4:5")
    theme = str(body.get("theme") or "solar")
    if theme not in ad_creatives.THEMES:
        return _error("Unknown colour theme {!r}.".format(theme))
    if not str(copy.get("headline") or "").strip():
        return _error("The ad needs a headline.")
    try:
        width, height = _ratio_size(ratio)
        picture = compose_ads._cover(_load_picture(str(body.get("picture") or "")), (width, height))
    except ValueError as exc:
        return _error(str(exc))
    points = [str(p).strip() for p in (copy.get("points") or []) if str(p).strip()][:3]
    with tempfile.TemporaryDirectory() as tmp:
        background, out = os.path.join(tmp, "picture.png"), os.path.join(tmp, "ad.png")
        picture.save(background, "PNG")
        compose_ads.compose_poster(
            background, out, ratio, str(copy.get("headline")).strip(), str(copy.get("subline") or "").strip(),
            str(copy.get("cta") or "Learn More").strip(), eyebrow=str(copy.get("eyebrow") or "").strip(),
            points=points, sticker=str(copy.get("sticker") or "").strip().upper()[:14], theme_name=theme)
        from PIL import Image

        with Image.open(out) as ad:
            image = _data_url(ad)
    return jsonify({"image": image, "format": ratio, "theme": theme, "size": [width, height]})


@app.route("/api/generate-picture", methods=["POST"])
def generate_picture():
    """
    A new text-free picture from an image prompt, at the format's size.

    With candidates > 1 the pictures render in parallel, Gemini vision scores each against
    the prompt (image_generator.picture_quality), and the best one is returned. The winner
    gets a light sharpen and contrast polish.
    """
    from concurrent.futures import ThreadPoolExecutor

    from image_generator import generate_images, picture_quality
    from trend_pipeline.gemini_client import get_client

    body = request.get_json(silent=True) or {}
    prompt = str(body.get("prompt") or "").strip()
    if len(prompt) < 40:
        return _error("Write an image prompt of at least a sentence.")
    model = IMAGE_MODELS.get(str(body.get("model") or "klein9b"))
    if not model:
        return _error("Unknown image model.")
    try:
        candidates = max(1, min(3, int(body.get("candidates") or 1)))
    except (TypeError, ValueError):
        return _error("candidates must be 1, 2 or 3.")
    if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN):
        return _error("Image generation is not configured (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN).", 503)
    try:
        width, height = _ratio_size(str(body.get("format") or "4:5"))
    except ValueError as exc:
        return _error(str(exc))
    client = generate_images.CloudflareImageClient(
        config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN, model, config.CLOUDFLARE_IMAGE_STEPS)

    def render(_):
        return client.text_to_image(prompt=prompt, width=width, height=height,
                                    negative_prompt=generate_images.NEGATIVE_FOR_BACKGROUND)

    images, failures = [], []
    with ThreadPoolExecutor(max_workers=candidates) as pool:
        for future in [pool.submit(render, i) for i in range(candidates)]:
            try:
                images.append(future.result())
            except (RuntimeError, OSError) as exc:
                failures.append(str(exc))
    if not images:
        logger.error("Picture generation failed: %s", failures[0] if failures else "no image")
        return _error(failures[0] if failures else "No picture was generated.", 502)
    best, scores = 0, [None] * len(images)
    if len(images) > 1:
        gemini = get_client()
        best, scores = picture_quality.choose_best(images, prompt, gemini if gemini.online else None)
    picture = picture_quality.polish(images[best])
    return jsonify({"picture": _data_url(picture, quality=92), "model": model, "candidates": len(images),
                    "chosen": best, "scores": scores})


@app.route("/api/video", methods=["POST"])
def make_video():
    """A 10-second branded motion video ad (MP4) from the picture, copy and colour theme."""
    from image_generator import video_ads
    from prompt_generator import ad_creatives

    body = request.get_json(silent=True) or {}
    copy = body.get("copy") or {}
    theme = str(body.get("theme") or "solar")
    ratio = str(body.get("format") or "9:16")
    ratio = ratio if ratio in video_ads.VIDEO_SIZES else "9:16"  # link ads (1.91:1) become a vertical Reel
    if theme not in ad_creatives.THEMES:
        return _error("Unknown colour theme {!r}.".format(theme))
    if not str(copy.get("headline") or "").strip():
        return _error("The video needs a headline.")
    try:
        picture = _load_picture(str(body.get("picture") or ""))
    except ValueError as exc:
        return _error(str(exc))
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "ad.mp4")
        try:
            video_ads.render_video(picture, copy, out, theme, ratio)
        except (RuntimeError, OSError) as exc:
            logger.exception("Video rendering failed")
            return _error("Could not render the video: {}".format(exc), 502)
        with open(out, "rb") as handle:
            data = base64.b64encode(handle.read()).decode("ascii")
    width, height = video_ads.VIDEO_SIZES[ratio]
    return jsonify({"video": "data:video/mp4;base64," + data, "format": ratio, "size": [width, height],
                    "seconds": video_ads.DURATION})


@app.route("/api/scrape", methods=["POST"])
def scrape():
    """Scrape the given URLs and run them through Gate 1 and Gate 2."""
    from trend_pipeline.main import load_urls_payload, run_pipeline
    from trend_pipeline.web_export import slim_trend

    body = request.get_json(silent=True) or {}
    raw = body.get("urls") or []
    if isinstance(raw, str):
        raw = raw.splitlines()
    urls = [u.strip().strip("\"',") for u in raw if str(u).strip().strip("\"',")]
    urls = [u for u in urls if u.startswith(("http://", "https://"))][:8]
    if not urls:
        return _error("Paste at least one http(s) link.")
    try:
        payload = load_urls_payload(urls)
        trends = run_pipeline(payload, print_json=False, user_submitted_url=True) or []
    except Exception as exc:  # scraping is best effort: report, don't crash the page
        logger.exception("Scrape failed")
        return _error("Could not process these links: {}".format(exc), 502)
    return jsonify({"trends": [slim_trend(t) for t in trends], "urls": urls})


@app.route("/api/brief", methods=["POST"])
def brief():
    """Write the R2C ad copy and image prompt for one trend in one format (Gemini + rule checks)."""
    from prompt_generator import ad_creatives
    from trend_pipeline.gemini_client import get_client

    body = request.get_json(silent=True) or {}
    trend = body.get("trend") or {}
    ratio = str(body.get("format") or "4:5")
    theme = str(body.get("theme") or "solar")
    ad_type = FORMAT_AD_TYPE.get(ratio)
    if not trend.get("topic_title") or not ad_type:
        return _error("Pick a trend and a format.")
    client = get_client()
    if not client.online:
        return _error("Ad copy needs GEMINI_API_KEY to be configured.", 503)
    records = ad_creatives.generate_trend_creatives(trend, client)
    record = next((r for r in records if r["ad_type"] == ad_type), None)
    if not record or record.get("status") != "success":
        return _error("Gemini's ad copy did not pass the checks: {}".format(
            (record or {}).get("error_message") or "no result"), 502)
    post = record["post"]
    return jsonify({
        "copy": {k: post.get(k) for k in ("audience", "eyebrow", "headline", "subline", "points", "cta",
                                           "caption", "hashtags")},
        "concept": (post.get("visual") or {}).get("concept"),
        "image_prompt": ad_creatives.compose_image_prompt(ad_type, post.get("visual") or {}, theme),
        "ad_type": ad_type, "placement": record["placement"],
    })


@app.route("/api/generate-ad", methods=["POST"])
def generate_ad():
    """Legacy endpoint: render a prompt with the configured image client and save it."""
    from image_generator import generate_images

    payload = request.get_json(silent=True) or {}
    prompt = str(payload.get("prompt") or "").strip()
    if len(prompt) < 40:
        return _error("Write an image brief of at least a sentence before generating.")
    ratio = FORMAT_ASPECT_RATIO.get(str(payload.get("format") or ""), "1:1")
    width, height = config.AD_ASPECT_SIZES[ratio]
    try:
        client = generate_images.create_image_client()
        image = client.text_to_image(prompt=prompt, width=width, height=height)
        filename = "ad_{}_{}.png".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f"), ratio.replace(":", "x"))
        image.save(os.path.join(config.GENERATED_ADS_DIR, filename), format="PNG")
    except (RuntimeError, ValueError, OSError) as exc:
        logger.error("Image generation failed: %s", exc)
        return _error(str(exc), 502)
    return jsonify({"image_url": "/api/image/{}".format(filename), "aspect_ratio": ratio,
                    "model": getattr(client, "model", None)})


@app.route("/api/image/<path:filename>")
def serve_generated_image(filename):
    return send_from_directory(config.GENERATED_ADS_DIR, filename)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    print("\nR2C.Ai Ad Studio running at http://127.0.0.1:5000\n")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)
