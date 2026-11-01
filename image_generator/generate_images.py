"""
Image stage: one text-free background image per post, through Cloudflare
Workers AI (default model FLUX.2 Klein 4B), at the aspect ratio of the post's
AIDA stage (config.AD_STAGE_ASPECT_RATIO). Needs CLOUDFLARE_ACCOUNT_ID and
CLOUDFLARE_API_TOKEN in .env. Ad copy is typeset over the image separately.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from trend_pipeline import config

logger = logging.getLogger("image_generator")

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
CLOUDFLARE_MAX_PROMPT_CHARS = 2048
CLOUDFLARE_MAX_STEPS = 8
CLOUDFLARE_TIMEOUT_SECONDS = 120
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def fit_prompt(prompt: str, limit: int = CLOUDFLARE_MAX_PROMPT_CHARS) -> str:
    """Trim a brief to the provider limit, ending on a sentence where possible."""
    prompt = " ".join(prompt.split())
    if len(prompt) <= limit:
        return prompt
    cut = prompt[:limit]
    end = max(cut.rfind(". "), cut.rfind("; "))
    return cut[: end + 1] if end > limit // 2 else cut.rsplit(" ", 1)[0]


def multipart_body(fields: Dict[str, Any]) -> tuple:
    """Encode plain form fields as multipart/form-data. Returns (body, content_type)."""
    boundary = uuid.uuid4().hex
    parts = [
        "--{}\r\nContent-Disposition: form-data; name=\"{}\"\r\n\r\n{}\r\n".format(boundary, key, value)
        for key, value in fields.items()
    ]
    body = ("".join(parts) + "--{}--\r\n".format(boundary)).encode("utf-8")
    return body, "multipart/form-data; boundary=" + boundary


class CloudflareImageClient:
    """
    Text-to-image through the Cloudflare Workers AI REST API.

    Three request shapes, verified against the live API:
      - FLUX.2 models: multipart form with prompt, width, height -> any aspect ratio.
      - FLUX.1 schnell: JSON with prompt, steps -> always 1024x1024; width and
        height are ignored and "seed" is rejected.
      - Stable Diffusion XL: JSON with prompt, negative_prompt, width, height,
        num_steps (max 20); the response is the raw PNG, not JSON.
    """

    provider = "cloudflare-workers-ai"

    def __init__(self, account_id: str, api_token: str, model: str, steps: int) -> None:
        self.url = CLOUDFLARE_API.format(account=account_id, model=model)
        self.model = model
        self._token = api_token
        self.steps = max(1, min(steps, CLOUDFLARE_MAX_STEPS))

    @property
    def supports_size(self) -> bool:
        return "flux-2" in self.model

    @property
    def is_sdxl(self) -> bool:
        return "stable-diffusion-xl" in self.model

    def text_to_image(self, prompt: str, width: Optional[int] = None,
                      height: Optional[int] = None, **_ignored: Any):
        """
        Return a PIL image. Negative-prompt and guidance arguments are accepted
        and ignored: these FLUX endpoints do not take them, and the briefs
        already ask for no text, logos or watermarks.
        """
        from PIL import Image

        if self.is_sdxl:
            payload = {"prompt": fit_prompt(prompt), "num_steps": 20, "guidance": 7.5}
            if width and height:
                payload.update(width=int(width) // 8 * 8, height=int(height) // 8 * 8)
            if _ignored.get("negative_prompt"):
                payload["negative_prompt"] = _ignored["negative_prompt"]
            body = self._post(json.dumps(payload).encode("utf-8"), "application/json")
        elif self.supports_size:
            fields: Dict[str, Any] = {"prompt": fit_prompt(prompt)}
            if width and height:
                fields.update(width=int(width), height=int(height))
            body = self._post(*multipart_body(fields))
        else:
            payload = {"prompt": fit_prompt(prompt), "steps": self.steps}
            body = self._post(json.dumps(payload).encode("utf-8"), "application/json")
        if isinstance(body, bytes):  # SDXL answers with the PNG itself
            image = Image.open(io.BytesIO(body))
            image.load()
            return image
        try:
            image_b64 = body["result"]["image"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Cloudflare response had no image: {}".format(
                _cloudflare_errors(body) or str(body)[:200])) from exc
        image = Image.open(io.BytesIO(base64.b64decode(image_b64)))
        image.load()
        return image

    def _post(self, data: bytes, content_type: str, attempts: int = 3) -> Any:
        """JSON response as a dict, or raw bytes when the model returns an image directly."""
        for attempt in range(1, attempts + 1):
            request = Request(self.url, data=data, method="POST", headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": content_type,
            })
            try:
                with urlopen(request, timeout=CLOUDFLARE_TIMEOUT_SECONDS) as response:
                    headers = getattr(response, "headers", None) or {}
                    if (headers.get("Content-Type") or "").startswith("image/"):
                        return response.read()
                    return json.load(response)
            except HTTPError as exc:
                detail = _cloudflare_errors(_read_json(exc)) or exc.reason
                if exc.code in RETRYABLE_STATUS and attempt < attempts:
                    logger.warning("Cloudflare HTTP %s (%s); retrying", exc.code, detail)
                    time.sleep(2 ** attempt)
                    continue
                hint = " (daily free allocation used up?)" if exc.code == 429 else ""
                raise RuntimeError("Cloudflare HTTP {}: {}{}".format(exc.code, detail, hint)) from exc
            except (URLError, TimeoutError) as exc:
                if attempt < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError("Could not reach Cloudflare: {}".format(exc)) from exc
        raise RuntimeError("Cloudflare request failed")


class HuggingFaceImageClient:
    """
    Text-to-image through Hugging Face Inference Providers (needs HF_TOKEN in .env).

    Hugging Face routes the call to a provider that hosts the model, e.g.
    stabilityai/stable-diffusion-3.5-medium on Replicate or Qwen/Qwen-Image-2512
    on fal-ai; usage is billed to the Hugging Face account.
    """

    provider = "huggingface-inference-providers"

    def __init__(self, model: str, token: str, hf_provider: str = "auto") -> None:
        from huggingface_hub import InferenceClient

        self.model = model
        self.hf_provider = hf_provider
        self._client = InferenceClient(provider=hf_provider, api_key=token)

    def text_to_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None,
                      negative_prompt: Optional[str] = None, **_ignored: Any):
        kwargs: Dict[str, Any] = {"model": self.model}
        if width and height:
            kwargs.update(width=int(width) // 16 * 16, height=int(height) // 16 * 16)
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt
        try:
            return self._client.text_to_image(" ".join(prompt.split()), **kwargs)
        except Exception as exc:  # surface the provider's message (quota, gating, size)
            raise RuntimeError("Hugging Face ({} via {}): {}".format(self.model, self.hf_provider, exc)) from exc


class HuggingFaceSpaceClient:
    """
    Text-to-image through a model's official Hugging Face Space (Gradio app on free
    ZeroGPU hardware). Free for a logged-in account within a daily GPU-time quota;
    used when the paid Inference Providers route has no credits.

    Spaces differ in their inputs: some take width/height, others an aspect-ratio
    choice. `aspects` lists the choices; the closest one to the requested size is used.
    """

    provider = "huggingface-space"

    def __init__(self, space: str, model: str, token: str, steps: Optional[int] = None,
                 aspects: Optional[Sequence[str]] = None, extra: Optional[Dict[str, Any]] = None) -> None:
        from gradio_client import Client

        self.space, self.model, self.steps = space, model, steps
        self.aspects = list(aspects or [])
        self.extra = dict(extra or {})
        self._client = Client(space, token=token, verbose=False)

    def _closest_aspect(self, width: int, height: int) -> str:
        def value(choice: str) -> float:
            w, h = choice.split(":")
            return float(w) / float(h)
        return min(self.aspects, key=lambda c: abs(value(c) - width / height))

    def text_to_image(self, prompt: str, width: Optional[int] = None, height: Optional[int] = None,
                      negative_prompt: Optional[str] = None, **_ignored: Any):
        from PIL import Image

        kwargs: Dict[str, Any] = dict(self.extra, prompt=" ".join(prompt.split()), randomize_seed=True)
        if self.steps:
            kwargs["num_inference_steps"] = self.steps
        if self.aspects:
            kwargs["aspect_ratio"] = self._closest_aspect(width or 1024, height or 1024)
        elif width and height:
            kwargs.update(width=int(width) // 16 * 16, height=int(height) // 16 * 16)
            if negative_prompt is not None:
                kwargs["negative_prompt"] = negative_prompt
        try:
            output = self._client.predict(api_name="/infer", **kwargs)
        except Exception as exc:  # quota exhausted, Space asleep or busy
            raise RuntimeError("Hugging Face Space {}: {}".format(self.space, exc)) from exc
        path = output[0] if isinstance(output, (list, tuple)) else output
        if isinstance(path, dict):
            path = path.get("path") or path.get("url")
        image = Image.open(path)
        image.load()
        return image


def _read_json(exc: HTTPError) -> Any:
    try:
        return json.loads(exc.read().decode("utf-8", "replace"))
    except (ValueError, OSError):
        return None


def _cloudflare_errors(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return "; ".join(str(e.get("message", e)) for e in body.get("errors") or [] if e)


def load_prompts(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError("Prompt JSON must be an array of prompt objects.")
    return payload


def create_image_client():
    if config.IMAGE_PROVIDER == "cloudflare":
        if not (config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN):
            raise RuntimeError(
                "IMAGE_PROVIDER=cloudflare needs CLOUDFLARE_ACCOUNT_ID and "
                "CLOUDFLARE_API_TOKEN in .env."
            )
        return CloudflareImageClient(
            config.CLOUDFLARE_ACCOUNT_ID,
            config.CLOUDFLARE_API_TOKEN,
            config.CLOUDFLARE_IMAGE_MODEL,
            config.CLOUDFLARE_IMAGE_STEPS,
        )
    raise RuntimeError(
        "Unsupported IMAGE_PROVIDER {!r}: set IMAGE_PROVIDER=cloudflare with "
        "CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN in .env.".format(config.IMAGE_PROVIDER)
    )


def _safe_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "trend"


def aspect_for_record(record: Dict[str, Any]) -> tuple:
    """(ratio label, width, height): the record's own aspect_ratio (ad creatives), else its AIDA stage's."""
    ratio = record.get("aspect_ratio")
    if ratio in config.AD_ASPECT_SIZES:
        width, height = config.AD_ASPECT_SIZES[ratio]
        return ratio, width, height
    return aspect_for_stage(record.get("aida_stage"))


def aspect_for_stage(stage: Optional[str]) -> tuple:
    """(ratio label, width, height) for an AIDA stage's image; unknown stages get the default size."""
    ratio = config.AD_STAGE_ASPECT_RATIO.get(str(stage or ""))
    if ratio and ratio in config.AD_ASPECT_SIZES:
        width, height = config.AD_ASPECT_SIZES[ratio]
        return ratio, width, height
    return "custom", config.AD_IMAGE_WIDTH, config.AD_IMAGE_HEIGHT


NEGATIVE_FOR_BACKGROUND = (
    "text, letters, words, typography, logo, watermark, signature, unreadable labels, fake interface, "
    "extra fingers, distorted hands, blurry subject, low detail, cluttered composition"
)
NEGATIVE_FOR_FULL_AD = (
    "misspelled text, gibberish letters, extra words, small print, watermark, fake interface, "
    "extra fingers, distorted hands, blurry, low detail, cluttered composition"
)


def _render(image_client: Any, prompt: str, width: int, height: int, negative: str, attempts: int = 3):
    """Render once; Cloudflare's safety filter flags some outputs at random, so retry those."""
    for attempt in range(1, attempts + 1):
        try:
            return image_client.text_to_image(
                prompt=prompt, width=width, height=height,
                num_inference_steps=config.AD_IMAGE_STEPS, negative_prompt=negative, guidance_scale=3.5,
            )
        except RuntimeError as exc:
            if "flagged" not in str(exc).lower() or attempt == attempts:
                raise
            logger.warning("Image output flagged by the provider (attempt %d/%d); retrying.", attempt, attempts)


def _save(image: Any, path: str) -> None:
    save = getattr(image, "save", None)
    if not callable(save):
        raise TypeError("Image client returned an image that cannot be saved.")
    save(path, format="PNG")


def generate_image_record(
    prompt_record: Dict[str, Any],
    image_client: Any,
    images_dir: str,
    text_checker: Optional[Callable[[Any], Optional[str]]] = None,
) -> Dict[str, Any]:
    """
    Generate one record's image.

    Ad creatives in "ai" render mode are finished ads drawn by the model, text
    included. `text_checker` (image to transcript, e.g. Gemini) reads the text
    back; a misspelled ad is redrawn up to AD_TEXT_RETRIES times, and if it is
    still wrong the ad is rebuilt as a text-free picture with the text typeset
    by code, so a misspelled ad is never delivered.
    """
    result: Dict[str, Any] = {
        "trend_id": prompt_record.get("trend_id"),
        "rank": prompt_record.get("rank"),
        "topic_title": prompt_record.get("topic_title"),
        "social_post": prompt_record.get("post"),
        "image_prompt": prompt_record.get("image_prompt")
        or (
            prompt_record.get("post", {}).get("image_prompt")
            if isinstance(prompt_record.get("post"), dict)
            else None
        ),
        "aida_stage": prompt_record.get("aida_stage"),
        "ad_type": prompt_record.get("ad_type"),
        "placement": prompt_record.get("placement"),
        "creative_angle": prompt_record.get("creative_angle"),
        "render_mode": prompt_record.get("render_mode"),
        "theme": prompt_record.get("theme"),
        "image_provider": getattr(image_client, "provider", "injected-client"),
        "image_model": getattr(image_client, "model", None),
        "aspect_ratio": None,
        "image_size": None,
        "image_path": None,
        "image_url": None,
        "status": "failed",
        "error_message": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        if prompt_record.get("status") != "success":
            raise ValueError(
                "Prompt record is not successful: {}".format(
                    prompt_record.get("error_message") or "no prompt available"
                )
            )
        prompt = result["image_prompt"]
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt record is missing image_prompt.")
        if not 500 <= len(prompt.strip()) <= 2400:
            raise ValueError(
                "Image prompt must be a detailed 500-2400 character visual brief."
            )
        ratio, width, height = aspect_for_record(prompt_record)
        os.makedirs(images_dir, exist_ok=True)
        # One trend has several posts; name by type/stage and ratio so none overwrite another.
        filename = "{}_{}_{}.png".format(
            _safe_filename(str(prompt_record.get("trend_id") or "trend")),
            _safe_filename(str(prompt_record.get("ad_type") or prompt_record.get("aida_stage") or "post")),
            ratio.replace(":", "x").replace(".", "_"),
        )
        full_ad = bool(prompt_record.get("ad_type") and prompt_record.get("render_mode") == "ai"
                       and prompt_record.get("ad_text"))
        # Branded ads: the finished ad goes in images_dir, its text-free picture in pictures/.
        picture_dir = (os.path.join(images_dir, "pictures")
                       if prompt_record.get("ad_type") and not full_ad else images_dir)
        os.makedirs(picture_dir, exist_ok=True)
        image_path = os.path.abspath(os.path.join(picture_dir, filename))
        result["aspect_ratio"] = ratio

        if full_ad:
            from .text_check import check_ad_text

            checks: List[Dict[str, Any]] = []
            result["text_checks"] = checks
            accepted = None
            for attempt in range(1, 2 + max(0, config.AD_TEXT_RETRIES)):
                image = _render(image_client, prompt, width, height, NEGATIVE_FOR_FULL_AD)
                if text_checker is None:
                    result["text_check"] = "unverified (no text checker)"
                    accepted = image
                    break
                transcript = text_checker(image)
                if transcript is None:
                    result["text_check"] = "unverified (Gemini unavailable)"
                    accepted = image
                    break
                ok, problems = check_ad_text(transcript, prompt_record["ad_text"])
                checks.append({"attempt": attempt, "transcript": transcript, "problems": problems})
                if ok:
                    result["text_check"] = "passed"
                    accepted = image
                    break
                logger.warning("Ad text check failed for %s %s (attempt %d): %s",
                               result["trend_id"], result["ad_type"], attempt, "; ".join(problems))
            if accepted is not None:
                _save(accepted, image_path)
                result["ad_path"] = image_path
                if prompt_record.get("logo_corner"):
                    from .compose_ads import place_logo

                    # The AI left this corner empty; stamp the real R2C logo there.
                    try:
                        result["ad_path"] = place_logo(
                            image_path, os.path.splitext(image_path)[0] + "_ad.png",
                            prompt_record["logo_corner"])
                    except Exception as exc:
                        result["ad_error"] = "{}: {}".format(type(exc).__name__, exc)
                        logger.exception("Could not place the logo for %s", result["trend_id"])
            else:
                # Still misspelled: draw the picture without text and typeset the text instead.
                result["text_check"] = "failed, used text overlay"
                background = prompt_record.get("background_prompt") or prompt
                accepted = _render(image_client, background, width, height, NEGATIVE_FOR_BACKGROUND)
                _save(accepted, image_path)
        else:
            accepted = _render(image_client, prompt, width, height, NEGATIVE_FOR_BACKGROUND)
            _save(accepted, image_path)

        size = getattr(accepted, "size", None)
        result["image_size"] = list(size) if size else [width, height]
        result["image_path"] = image_path
        result["status"] = "success"
        # Text-free ad pictures (overlay mode, or the fallback above) get their
        # headline, subline, CTA and R2C.Ai branding typeset by code.
        post = result.get("social_post") or {}
        if prompt_record.get("ad_type") and post.get("headline") and not result.get("ad_path"):
            from .compose_ads import compose_record

            try:
                result["ad_path"] = compose_record(
                    result, out_dir=images_dir if os.path.dirname(image_path).endswith("pictures") else None)
            except Exception as exc:  # the picture is still useful if typesetting fails
                result["ad_error"] = "{}: {}".format(type(exc).__name__, exc)
                logger.exception("Could not compose the finished ad for %s", result["trend_id"])
    except Exception as exc:
        result["error_message"] = "{}: {}".format(type(exc).__name__, exc)
        logger.exception("Could not generate image for trend %s", result["trend_id"])
    return result


def write_results(path: str, results: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def run(
    input_path: str = config.GENERATED_PROMPTS_JSON,
    output_path: str = config.GENERATED_ADS_JSON,
    images_dir: str = config.GENERATED_ADS_DIR,
    image_client: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    prompt_records = load_prompts(input_path)
    client = image_client if image_client is not None else create_image_client()
    text_checker = None
    if any(r.get("render_mode") == "ai" for r in prompt_records):
        from trend_pipeline.gemini_client import get_client

        gemini = get_client()
        if gemini.online:
            text_checker = gemini.read_image_text
        else:
            logger.warning("Gemini unavailable: AI-designed ads will not have their text checked.")
    results: List[Dict[str, Any]] = []
    for prompt_record in prompt_records:
        results.append(generate_image_record(prompt_record, client, images_dir, text_checker))
        write_results(output_path, results)
    successes = sum(item["status"] == "success" for item in results)
    logger.info(
        "Image generation (%s) finished: %d succeeded, %d failed; manifest: %s",
        getattr(client, "provider", "injected client"),
        successes,
        len(results) - successes,
        output_path,
    )
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate ad background images through Cloudflare Workers AI."
    )
    parser.add_argument("--input", default=config.GENERATED_PROMPTS_JSON)
    parser.add_argument("--output", default=config.GENERATED_ADS_JSON)
    parser.add_argument("--images-dir", default=config.GENERATED_ADS_DIR)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
        stream=sys.stdout,
    )
    try:
        results = run(args.input, args.output, args.images_dir)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Image generation could not start: %s", exc)
        return 2
    return 0 if all(item["status"] == "success" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
