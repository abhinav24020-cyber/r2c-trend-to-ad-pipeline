"""
picture_quality.py — Pick the best of several generated pictures, and polish the winner.

The image model is stochastic: two renders of the same prompt differ a lot in
defects (gibberish writing, warped hands) and in how well they follow the brief.
Gemini vision scores each candidate with the rubric used in the model benchmark
(scripts/compare_score_models.py), and the highest overall score wins.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("picture_quality")

CRITERIA = ("adherence", "realism", "catchiness", "artifacts")
RUBRIC = """You are judging an AI-generated picture for a social media ad. It was generated from the PROMPT below.
Score each criterion from 1 (very poor) to 10 (excellent):
- adherence: how faithfully the picture shows the subject, action, setting and colour palette in the prompt
- realism: how much it looks like a real photo or a professionally designed illustration, NOT obviously AI-generated (plastic skin, warped hands or faces, melted objects lower this)
- catchiness: would it stop someone scrolling Instagram or LinkedIn
- artifacts: 10 = no visible defects; lower for gibberish text or letters, extra fingers, distorted faces, broken objects
Reply with JSON only: {"adherence": n, "realism": n, "catchiness": n, "artifacts": n, "note": "one short sentence"}

PROMPT:
"""


def parse_scores(reply: Optional[str]) -> Optional[Dict[str, Any]]:
    if not reply:
        return None
    try:
        data = json.loads(re.sub(r"^```(?:json)?|```$", "", reply.strip()))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    try:
        scores = {c: max(1.0, min(10.0, float(data[c]))) for c in CRITERIA}
    except (KeyError, TypeError, ValueError):
        return None
    scores["overall"] = round(sum(scores[c] for c in CRITERIA) / len(CRITERIA), 2)
    scores["note"] = str(data.get("note") or "")[:200]
    return scores


def score_picture(image: Any, prompt: str, client: Any) -> Optional[Dict[str, Any]]:
    """Gemini vision scores for one picture, or None when Gemini is unavailable."""
    judge = getattr(client, "judge_image", None)
    if judge is None:
        return None
    thumb = image.convert("RGB").copy()
    thumb.thumbnail((1024, 1024))
    return parse_scores(judge(thumb, RUBRIC + prompt))


def choose_best(images: Sequence[Any], prompt: str, client: Any) -> Tuple[int, List[Optional[Dict[str, Any]]]]:
    """Index of the best picture and every candidate's scores. Unscored candidates rank last."""
    scores = [score_picture(image, prompt, client) if client is not None else None for image in images]
    best = max(range(len(images)), key=lambda i: (scores[i] or {}).get("overall", -1.0))
    return best, scores


def polish(image: Any) -> Any:
    """Light sharpening and contrast: crisper print-like detail without changing the look."""
    from PIL import ImageEnhance, ImageFilter

    image = image.convert("RGB").filter(ImageFilter.UnsharpMask(radius=1.6, percent=60, threshold=3))
    return ImageEnhance.Contrast(image).enhance(1.05)
