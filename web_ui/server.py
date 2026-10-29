"""Loopback-only web app for the R2C URL-to-ad workflow."""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

from trend_pipeline import config

PROJECT_DIR = config.PROJECT_DIR
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
RUNS_DIR = os.path.join(config.DATA_DIR, "ui_runs")
MAX_REQUEST_BYTES = 256 * 1024
MAX_URLS = 8
MAX_DISCOVERY_QUERIES = 12
RUN_ID_PATTERN = re.compile(r"^[a-f0-9]{32}$")
_PIPELINE_LOCK = threading.Lock()
_CONFIG_PATHS = (
    "DATA_DIR",
    "VALIDATED_SIGNALS_JSON",
    "SIGNALS_DB",
    "POST_HISTORY_JSON",
    "REJECTED_SIGNALS_JSON",
    "RANKED_TRENDS_JSON",
    "GENERATED_PROMPTS_JSON",
    "GENERATED_ADS_JSON",
    "GENERATED_ADS_DIR",
    "STORAGE_BACKEND",
)

logger = logging.getLogger("web_ui")


def parse_urls(value: Any) -> List[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = value.splitlines()
    else:
        raise ValueError("Add one or more public webpage URLs.")
    urls: List[str] = []
    for item in candidates:
        if not isinstance(item, str):
            continue
        url = item.strip().strip(",").strip().strip("\"'")
        if url:
            urls.append(url)
    if not urls:
        raise ValueError("Add at least one public webpage URL.")
    if len(urls) > MAX_URLS:
        raise ValueError("For one run, enter no more than {} URLs.".format(MAX_URLS))
    return urls


def parse_queries(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = value.splitlines()
    else:
        raise ValueError("Search topics must be a list of phrases or one phrase per line.")

    queries: List[str] = []
    seen = set()
    for item in candidates:
        if not isinstance(item, str):
            continue
        query = " ".join(item.split())
        if not query or query.casefold() in seen:
            continue
        if len(query) > 120:
            raise ValueError("Each search topic must be no longer than 120 characters.")
        seen.add(query.casefold())
        queries.append(query)
    if len(queries) > MAX_DISCOVERY_QUERIES:
        raise ValueError(
            "Use no more than {} custom search topics.".format(MAX_DISCOVERY_QUERIES)
        )
    return queries


def _configure_run(run_dir: str) -> Dict[str, Any]:
    previous = {name: getattr(config, name) for name in _CONFIG_PATHS}
    config.DATA_DIR = run_dir
    config.VALIDATED_SIGNALS_JSON = os.path.join(run_dir, "validated_signals.json")
    config.SIGNALS_DB = os.path.join(run_dir, "signals.db")
    config.POST_HISTORY_JSON = os.path.join(run_dir, "post_history.json")
    config.REJECTED_SIGNALS_JSON = os.path.join(run_dir, "rejected_signals.json")
    config.RANKED_TRENDS_JSON = os.path.join(run_dir, "ranked_trends.json")
    config.GENERATED_PROMPTS_JSON = os.path.join(run_dir, "generated_prompts.json")
    config.GENERATED_ADS_JSON = os.path.join(run_dir, "generated_ads.json")
    config.GENERATED_ADS_DIR = os.path.join(run_dir, "generated_images")
    config.STORAGE_BACKEND = "json"
    return previous


def _restore_config(previous: Dict[str, Any]) -> None:
    for name, value in previous.items():
        setattr(config, name, value)


def _read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_json(path: str, value: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _process_topics(
    run_id: str,
    run_dir: str,
    scraped: Dict[str, List[Dict[str, Any]]],
    sources: List[Dict[str, Any]],
    user_submitted_url: bool,
) -> Dict[str, Any]:
    _write_json(os.path.join(run_dir, "1_input.json"), scraped)
    from trend_pipeline.main import run_pipeline

    trends = run_pipeline(scraped, print_json=False, user_submitted_url=user_submitted_url)
    if not trends:
        raise ValueError(
            "Sources were collected, but no usable topics survived the trend pipeline."
        )
    _write_json(os.path.join(run_dir, "ranked_trends.json"), trends)
    topics = [
        {
            "trend_id": item["trend_id"],
            "rank": item.get("rank"),
            "title": item.get("topic_title", "Research topic"),
            "summary": item.get("topic_summary", ""),
            "score": item.get("composite_trend_score"),
            "platforms": item.get("cross_platform_validation", {}).get("platforms", []),
            "source_count": item.get("supporting_signal_count", 0),
            "sources": item.get("source_attribution", []),
        }
        for item in trends
    ]
    _write_json(
        os.path.join(run_dir, "run.json"),
        {"run_id": run_id, "sources": sources, "topics": topics},
    )
    return {"run_id": run_id, "sources": sources, "topics": topics}


def create_topics(payload: Dict[str, Any]) -> Dict[str, Any]:
    urls = parse_urls(payload.get("urls"))
    run_id = uuid.uuid4().hex
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=False)

    with _PIPELINE_LOCK:
        previous = _configure_run(run_dir)
        try:
            from trend_pipeline.url_scraper import scrape_url

            scraped: Dict[str, List[Dict[str, Any]]] = {}
            sources: List[Dict[str, Any]] = []
            for url in urls:
                try:
                    result = scrape_url(url)
                    for platform, records in result.items():
                        scraped.setdefault(platform, []).extend(records)
                    records = next(iter(result.values()), [])
                    record = records[0] if records else {}
                    sources.append({
                        "url": url,
                        "status": "success",
                        "title": record.get("title") or url,
                    })
                except (OSError, ValueError, RuntimeError) as exc:
                    logger.info("URL was not included (%s): %s", url, exc)
                    sources.append({"url": url, "status": "failed", "error": str(exc)})

            if not scraped:
                details = "; ".join(
                    "{}: {}".format(item["url"], item.get("error", "could not read page"))
                    for item in sources
                )
                raise ValueError("None of the pages could be scraped. {}".format(details))

            return _process_topics(
                run_id, run_dir, scraped, sources, user_submitted_url=True
            )
        finally:
            _restore_config(previous)


def create_discovered_topics(payload: Dict[str, Any]) -> Dict[str, Any]:
    queries = parse_queries(payload.get("queries"))
    run_id = uuid.uuid4().hex
    run_dir = os.path.join(RUNS_DIR, run_id)
    os.makedirs(run_dir, exist_ok=False)

    with _PIPELINE_LOCK:
        previous = _configure_run(run_dir)
        try:
            from web_crawler import crawler, settings

            crawl_result = crawler.crawl(queries or settings.SEED_QUERIES, use_radar=True)
            scraped = crawl_result.payload()
            if not scraped:
                stats = getattr(crawl_result, "stats", {})
                raise RuntimeError(
                    "Automatic discovery found no usable records. Crawl stats: {}".format(stats)
                )

            sources: List[Dict[str, Any]] = []
            for platform, records in scraped.items():
                for record in records[:8]:
                    title = next(
                        (
                            str(record[key]).strip()
                            for key in ("title", "query", "question", "name", "model_id")
                            if record.get(key)
                        ),
                        platform,
                    )
                    url = next(
                        (
                            str(record[key]).strip()
                            for key in ("url", "source_url", "link")
                            if record.get(key)
                        ),
                        "",
                    )
                    sources.append(
                        {"platform": platform, "url": url, "title": title, "status": "success"}
                    )
                    if len(sources) >= 80:
                        break
                if len(sources) >= 80:
                    break

            response = _process_topics(
                run_id, run_dir, scraped, sources, user_submitted_url=False
            )
            response["discovery"] = {
                "queries": crawl_result.queries,
                "trending_queries": [
                    {"query": query, "origin": origin}
                    for query, origin in crawl_result.trending_queries
                ],
                "stats": crawl_result.stats,
                "record_counts": {
                    platform: len(records) for platform, records in scraped.items()
                },
            }
            return response
        finally:
            _restore_config(previous)


def compose_campaign(payload: Dict[str, Any]) -> Dict[str, Any]:
    run_id = payload.get("run_id")
    trend_id = payload.get("trend_id")
    stage = payload.get("stage", "action")
    creative_angle = payload.get("creative_angle", "Evidence-led insight")
    theme = payload.get("theme", "Midnight cyan")
    platform = payload.get("platform", "LinkedIn")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("The selected run is not valid. Scrape the links again.")
    if not isinstance(trend_id, str) or not trend_id:
        raise ValueError("Choose a research topic before generating the campaign.")
    if stage not in {"awareness", "interest", "desire", "action"}:
        raise ValueError("Choose a valid AIDA stage.")
    run_dir = os.path.join(RUNS_DIR, run_id)
    if not os.path.isfile(os.path.join(run_dir, "ranked_trends.json")):
        raise ValueError("This topic run has expired. Scrape the links again.")

    with _PIPELINE_LOCK:
        previous = _configure_run(run_dir)
        try:
            from prompt_generator import generate_prompts
            from trend_pipeline.gemini_client import get_client

            if creative_angle not in generate_prompts.CREATIVE_ANGLES:
                raise ValueError("Choose a supported campaign concept angle.")
            trends = generate_prompts.load_trends(config.RANKED_TRENDS_JSON)
            trend = next((item for item in trends if item.get("trend_id") == trend_id), None)
            if trend is None:
                raise ValueError("The selected topic was not found in this run.")
            template = generate_prompts.make_post_request(
                trend, stage, creative_angle=creative_angle
            )
            prompt_suffix = (
                "\n\nCAMPAIGN CREATIVE DIRECTION\n"
                "Social platform: {}.\n"
                "Visual palette: {}.\n"
                "Keep the public-facing copy suitable for this platform. "
                "Return the required JSON schema only. For image_prompt, describe "
                "a premium editorial background illustration with no letters, words, "
                "logos, or readable text; the product UI will typeset the final ad."
            ).format(platform, theme)
            request = payload.get("generation_prompt")
            if not isinstance(request, str) or not request.strip():
                request = template + prompt_suffix
            if len(request) > 30000:
                raise ValueError("The prompt is too long (maximum 30,000 characters).")

            client = get_client()
            if not client.online:
                raise RuntimeError(
                    "Gemini is not available. Check GEMINI_API_KEY and the network, then try again."
                )
            response = client.generate_text(
                request, label="web_ui_generate_{}_campaign".format(stage)
            )
            if not response:
                raise RuntimeError("Gemini returned an empty response. Please try again.")
            post = generate_prompts.parse_and_validate_post(response, stage)
            image_prompt = payload.get("image_prompt")
            if isinstance(image_prompt, str) and image_prompt.strip():
                post["image_prompt"] = image_prompt.strip()
            record = {
                "trend_id": trend_id,
                "rank": trend.get("rank"),
                "topic_title": trend.get("topic_title"),
                "aida_stage": stage,
                "prompt_version": generate_prompts.PROMPT_VERSION,
                "post": post,
                "status": "success",
                "error_message": None,
            }
            _write_json(config.GENERATED_PROMPTS_JSON, [record])
            with open(
                os.path.join(run_dir, "generation_request.txt"), "w", encoding="utf-8"
            ) as handle:
                handle.write(request)
            return {
                "run_id": run_id,
                "trend_id": trend_id,
                "topic_title": trend.get("topic_title"),
                "stage": stage,
                "creative_angle": creative_angle,
                "platform": platform,
                "theme": theme,
                "generation_prompt": request,
                "post": post,
            }
        finally:
            _restore_config(previous)


def preview_prompt(payload: Dict[str, Any]) -> Dict[str, str]:
    run_id = payload.get("run_id")
    trend_id = payload.get("trend_id")
    stage = payload.get("stage", "action")
    creative_angle = payload.get("creative_angle", "Evidence-led insight")
    theme = payload.get("theme", "Midnight cyan")
    platform = payload.get("platform", "LinkedIn")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("The selected run is not valid. Scrape the links again.")
    if stage not in {"awareness", "interest", "desire", "action"}:
        raise ValueError("Choose a valid AIDA stage.")
    run_dir = os.path.join(RUNS_DIR, run_id)
    if not os.path.isfile(os.path.join(run_dir, "ranked_trends.json")):
        raise ValueError("This topic run has expired. Scrape the links again.")
    with _PIPELINE_LOCK:
        previous = _configure_run(run_dir)
        try:
            from prompt_generator import generate_prompts

            if creative_angle not in generate_prompts.CREATIVE_ANGLES:
                raise ValueError("Choose a supported campaign concept angle.")
            trends = generate_prompts.load_trends(config.RANKED_TRENDS_JSON)
            trend = next((item for item in trends if item.get("trend_id") == trend_id), None)
            if trend is None:
                raise ValueError("The selected topic was not found in this run.")
            prompt = generate_prompts.make_post_request(
                trend, stage, creative_angle=creative_angle
            )
            prompt += (
                "\n\nCAMPAIGN CREATIVE DIRECTION\n"
                "Social platform: {}.\n"
                "Visual palette: {}.\n"
                "Keep the public-facing copy suitable for this platform. "
                "Return the required JSON schema only. For image_prompt, describe "
                "a premium editorial background illustration with no letters, words, "
                "logos, or readable text; the product UI will typeset the final ad."
            ).format(platform, theme)
            return {"generation_prompt": prompt}
        finally:
            _restore_config(previous)


def generate_campaign_image(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Save the (possibly edited) image brief, then render it with Cloudflare Workers AI."""
    run_id = payload.get("run_id")
    prompt = payload.get("image_prompt")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("The selected run is not valid. Scrape the links again.")
    if not isinstance(prompt, str) or not 500 <= len(prompt.strip()) <= 2400:
        raise ValueError("The image brief must be 500-2400 characters.")
    run_dir = os.path.join(RUNS_DIR, run_id)
    prompt_path = os.path.join(run_dir, "generated_prompts.json")
    with _PIPELINE_LOCK:
        records = _read_json(prompt_path)
        if not isinstance(records, list) or not records:
            raise ValueError("Generate campaign copy before generating the image.")
        record = records[0]
        post = record.get("post")
        if not isinstance(post, dict):
            raise ValueError("The campaign post is unavailable. Generate it again.")
        post["image_prompt"] = prompt.strip()
        record["image_prompt"] = prompt.strip()
        _write_json(prompt_path, records)

        from image_generator import generate_images

        client = generate_images.create_image_client()
        result = generate_images.generate_image_record(
            record, client, os.path.join(run_dir, "images")
        )
        _write_json(os.path.join(run_dir, "generated_ads.json"), [result])
        if result["status"] != "success":
            raise RuntimeError(result.get("error_message") or "Image generation failed.")
        return {
            "image_url": "/api/runs/{}/image/0".format(run_id),
            "aspect_ratio": result.get("aspect_ratio"),
            "model": result.get("image_model"),
        }


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class R2CRequestHandler(BaseHTTPRequestHandler):
    server_version = "R2CStudio/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - %s", self.address_string(), format % args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: Any) -> None:
        self._send(status, _json_bytes(value))

    def _read_payload(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Invalid request size.") from exc
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request must contain JSON and stay under 256 KB.")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must be valid UTF-8 JSON.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request JSON must be an object.")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"})
            return
        match =re.fullmatch(r"/api/runs/([a-f0-9]{32})/image/(\d+)", path)
        if match:
            self._send_run_image(match.group(1), int(match.group(2)))
            return
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/studio.css": ("studio.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        resource = static_files.get(path)
        if resource:
            filename, content_type = resource
            try:
                with open(os.path.join(STATIC_DIR, filename), "rb") as handle:
                    self._send(HTTPStatus.OK, handle.read(), content_type)
            except OSError:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "UI asset is missing."})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})

    def _send_run_image(self, run_id: str, index: int) -> None:
        run_dir = os.path.join(RUNS_DIR, run_id)
        try:
            records = _read_json(os.path.join(run_dir, "generated_ads.json"))
            record = records[index]
            image_path = os.path.abspath(record["image_path"])
            if os.path.commonpath([os.path.abspath(run_dir), image_path]) != os.path.abspath(run_dir):
                raise ValueError("Image path is outside its run folder.")
            self._send_image_file(image_path)
        except (OSError, ValueError, IndexError, KeyError, TypeError, json.JSONDecodeError):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Generated image not found."})

    def _send_image_file(self, path: str) -> None:
        if not os.path.isfile(path):
            raise OSError("Image not found.")
        extension = os.path.splitext(path)[1].lower()
        content_type = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(extension, "application/octet-stream")
        with open(path, "rb") as handle:
            self._send(HTTPStatus.OK, handle.read(), content_type)

    def do_POST(self) -> None:
        handlers = {
            "/api/topics": create_topics,
            "/api/discover": create_discovered_topics,
            "/api/prompt-preview": preview_prompt,
            "/api/compose": compose_campaign,
            "/api/generate-image": generate_campaign_image,
        }
        handler = handlers.get(urlparse(self.path).path)
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "API route not found."})
            return
        try:
            payload = self._read_payload()
            result = handler(payload)
            self._send_json(HTTPStatus.OK, result)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except RuntimeError as exc:
            self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        except Exception:
            logger.exception("Web workflow request failed")
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "The workflow hit an unexpected error. See the server terminal for details."},
            )


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The R2C Studio server must bind to loopback only.")
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
    )
    os.makedirs(RUNS_DIR, exist_ok=True)
    server = ThreadingHTTPServer((host, port), R2CRequestHandler)
    print("R2C Studio is ready at http://{}:{}".format(host, port))
    print("API keys remain on this computer; stop the server with Ctrl+C.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping R2C Studio.")
    finally:
        server.server_close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Start the local R2C creative studio.")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("--port must be between 1024 and 65535.")
    serve(port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
