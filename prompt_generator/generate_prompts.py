"""Generate validated AIDA social posts and image briefs from ranked trends."""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from string import Formatter
from typing import Any, Dict, List, Optional, Sequence

from trend_pipeline import config
from trend_pipeline.gemini_client import get_client

logger = logging.getLogger("prompt_generator")

PROMPT_VERSION = "v2"
PROMPT_PACKAGE = "prompt_generator.prompts.v1"
AIDA_STAGES = ("awareness", "interest", "desire", "action")
CREATIVE_ANGLES = {
    "Evidence-led insight": (
        "Lead with the clearest specific finding or evidence in the supplied sources; "
        "explain why it matters without overstating it."
    ),
    "Method spotlight": (
        "Make the research method, benchmark, dataset, or evaluation approach the "
        "story; explain what it measures and what it does not establish."
    ),
    "Problem or research gap": (
        "Frame a documented limitation, unresolved question, or research gap. Do not "
        "invent customer pain, market demand, or a problem absent from the sources."
    ),
    "Evidence comparison": (
        "Compare approaches or findings only when the supplied evidence directly "
        "supports the comparison; never fabricate a baseline or quantified advantage."
    ),
    "Research-to-practice pathway": (
        "Describe one plausible research-to-practice implication and label it as a "
        "possibility unless deployment or commercial validation is explicitly evidenced."
    ),
    "Open question": (
        "Build an educational hook around a genuine open question raised by the "
        "evidence, then help the audience understand what further evidence is needed."
    ),
}
# Each AIDA stage of a trend gets a different creative angle, so the four posts
# for one trend are four different kinds of content (an insight, a method
# explainer, a research-to-practice possibility, a problem the reader can act
# on) instead of four rewrites of the same "evidence-led insight".
STAGE_CREATIVE_ANGLE = {
    "awareness": "Evidence-led insight",
    "interest": "Method spotlight",
    "desire": "Research-to-practice pathway",
    "action": "Problem or research gap",
}
ORIENTATION = {"16:9": "wide landscape", "1:1": "square", "4:5": "portrait", "9:16": "tall vertical"}
# Each stage also gets its own visual medium and palette. Without this, Gemini
# wrote eight near-identical "editorial photo of a slate-grey lab workbench"
# briefs for two trends; the four images of one trend must read as four
# different creatives, not four crops of the same scene.
STAGE_VISUAL_STYLE = {
    "awareness": (
        "one bold conceptual photograph: a single striking focal object or scene from an "
        "unexpected angle, cinematic contrast, one dominant accent colour against a dark "
        "or very light field",
        "deep navy or near-black with one vivid accent (electric teal, signal orange or "
        "acid yellow)",
    ),
    "interest": (
        "a clean explanatory illustration: an isometric or cutaway render that shows HOW "
        "the method or system works as physical parts fitting together, flat even "
        "lighting, crisp edges, no photorealism",
        "paper-white or soft cream background with two saturated primaries (cobalt blue "
        "and coral) plus fine graphite line work",
    ),
    "desire": (
        "a warm human-scale scene: real people in a workplace, clinic, classroom, studio "
        "or field site using the outcome, candid documentary framing, natural light",
        "sunlit warm neutrals: sand, terracotta, olive and soft sky blue",
    ),
    "action": (
        "a graphic poster: large simple shapes, flat colour blocks and one iconic "
        "silhouette, strong vertical rhythm, subtle screen-print texture",
        "two-tone high contrast: ink black plus one saturated colour (magenta, emerald or "
        "cobalt), no gradients",
    ),
}
EVIDENCE_SOURCES = 5
EVIDENCE_EXCERPT_CHARS = 500
POST_FIELDS = {
    "aida_stage",
    "short_version",
    "long_version",
    "hook",
    "hashtags",
    "target_persona",
    "source_insight",
    "image_prompt",
}
DEFAULT_PERSONA = (
    "Researchers, professors, technology-transfer professionals, research "
    "commercialization teams, and industry partners."
)
BRAND_VOICE_NOTES = (
    "R2C.AI connects academic research with industry commercialization. Be "
    "authoritative, accessible, forward-thinking, and empowering. Prefer "
    "educational insight over promotional copy; never promise commercial success."
)
HOOK_PATTERNS = {
    "awareness": "Open with a precise research finding, tension, or useful question.",
    "interest": "Lead with a specific method, evidence point, or implication worth understanding.",
    "desire": "Lead with a credible potential outcome or research-to-industry possibility.",
    "action": "Lead with the practical next step the target persona can take.",
}
CTA_PATTERNS = {
    "awareness": "No CTA. Do not ask the reader to act.",
    "interest": "End with a reflective question or insight; no sales CTA.",
    "desire": "Use a soft invitation to consider, discuss, or explore a supported possibility.",
    "action": (
        "Include R2C.AI and ask for one measurable next step, such as reviewing "
        "one research profile or uploading one project summary."
    ),
}


def load_trends(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise ValueError("Input JSON must be an array of ranked trend objects.")
    return payload


def _context_for_trend(trend: Dict[str, Any], stage: str) -> Dict[str, str]:
    title = str(trend.get("topic_title", "")).strip()
    if not title:
        raise ValueError("Ranked trend is missing its required topic_title.")

    attribution = trend.get("source_attribution", [])
    if not isinstance(attribution, list):
        attribution = []
    # Longest excerpts first: articles carry the facts, short posts the reaction.
    ranked_sources = sorted(
        (s for s in attribution if isinstance(s, dict)),
        key=lambda s: -len(str(s.get("excerpt") or "")),
    )
    sources = []
    for source in ranked_sources[:EVIDENCE_SOURCES]:
        item = {
            "platform": source.get("platform"),
            "source_url": source.get("source_url"),
            "source_insight": source.get("source_insight"),
        }
        if source.get("excerpt"):
            item["excerpt"] = str(source["excerpt"])[:EVIDENCE_EXCERPT_CHARS]
        sources.append(item)
    evidence = {
        "summary": str(trend.get("topic_summary", "")).strip(),
        "key_facts": trend.get("topic_key_facts", []),
        "named_entities": trend.get("topic_entities", []),
        "keywords": trend.get("topic_keywords", []),
        "supporting_signal_count": trend.get("supporting_signal_count"),
        "cross_platform_validation": trend.get("cross_platform_validation", {}),
        "sources": sources,
    }
    persona = trend.get("target_persona")
    return {
        "cluster_label": title,
        "cluster_description": str(trend.get("topic_summary", "")).strip()
        or "No description supplied.",
        "evidence": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
        "target_persona": str(persona).strip() if persona else DEFAULT_PERSONA,
        "hook_pattern": HOOK_PATTERNS[stage],
        "cta_pattern": CTA_PATTERNS[stage],
        "brand_voice_notes": BRAND_VOICE_NOTES,
    }


def _stage_template(stage: str):
    if stage not in AIDA_STAGES:
        raise ValueError(
            "Unsupported AIDA stage {!r}; choose from {}.".format(
                stage, ", ".join(AIDA_STAGES)
            )
        )
    return importlib.import_module("{}.{}".format(PROMPT_PACKAGE, stage))


def make_post_request(
    trend: Dict[str, Any],
    stage: str,
    creative_angle: Optional[str] = None,
) -> str:
    """Compose the versioned stage system/developer prompts with shared context."""
    creative_angle = creative_angle or STAGE_CREATIVE_ANGLE.get(stage, "Evidence-led insight")
    if creative_angle not in CREATIVE_ANGLES:
        raise ValueError(
            "Unsupported creative angle {!r}; choose from {}.".format(
                creative_angle, ", ".join(CREATIVE_ANGLES)
            )
        )
    template = _stage_template(stage)
    context = _context_for_trend(trend, stage)
    fields = {
        field_name
        for _, field_name, _, _ in Formatter().parse(template.DEVELOPER_PROMPT)
        if field_name
    }
    missing = fields - context.keys()
    if missing:
        raise ValueError(
            "Prompt template {} is missing context values: {}".format(
                stage, ", ".join(sorted(missing))
            )
        )
    developer_prompt = template.DEVELOPER_PROMPT.format(**context)
    full_prompt = (
        "SYSTEM_PROMPT:\n{}\n\nDEVELOPER_PROMPT:\n{}\n\n"
        "CAMPAIGN CONCEPT STRATEGY\n"
        "Selected angle: {}\n"
        "{}\n"
        "Use this as a creative lens, not a factual source. Ground every assertion "
        "in the supplied evidence, keep the selected AIDA-stage requirements, and "
        "do not use urgency, testimonials, social proof, statistics, or product "
        "claims unless the evidence explicitly supports them.\n\n"
        "IMAGE-GENERATION BRIEF\n"
        "The image_prompt is sent to a FLUX text-to-image model and rendered as a {} "
        "{} image. Compose for that frame, overriding any other crop mentioned above: "
        "place the focal subject and the empty space for typeset copy where they work "
        "in a {} layout.\n"
        "Visual treatment for this {} post: {}. Palette: {}. Follow this treatment "
        "even if it differs from the stage rules above; it is what makes the four "
        "posts of one trend look like four different creatives.\n"
        "Pick a scene specific to this story and its named entities. Do not default to "
        "a laboratory workbench, desk, office or server rack unless the story itself is "
        "about that hardware, and never use glowing brains or floating network nodes.\n"
        "Write a standalone, vivid visual-generation prompt of 80-140 words (500-2400 "
        "characters). Put the medium, focal subject and composition first, then the "
        "concrete research-grounded visual metaphor, setting, perspective, "
        "foreground/background depth, materials, lighting and mood. Request no words, "
        "letters, numbers, handwriting, typography, logos, UI, chart labels, watermarks, "
        "or invented data. The studio typesets copy separately."
    )
    ratio = config.AD_STAGE_ASPECT_RATIO.get(stage, "1:1")
    orientation = ORIENTATION.get(ratio, "square")
    style, palette = STAGE_VISUAL_STYLE.get(stage, STAGE_VISUAL_STYLE["awareness"])
    return full_prompt.format(
        template.SYSTEM_PROMPT,
        developer_prompt,
        creative_angle,
        CREATIVE_ANGLES[creative_angle],
        ratio,
        orientation,
        orientation,
        stage,
        style,
        palette,
    )


def make_image_prompt_request(trend: Dict[str, Any], stage: str = "awareness") -> str:
    """Compatibility name for callers that need the composed content/image brief."""
    return make_post_request(trend, stage)


def parse_and_validate_post(response_text: str, stage: str) -> Dict[str, Any]:
    """Parse Gemini's JSON-only response and enforce the public post contract."""
    try:
        post = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError("Gemini response was not valid JSON: {}".format(exc)) from exc
    if not isinstance(post, dict):
        raise ValueError("Gemini response must be one JSON object.")
    if set(post) != POST_FIELDS:
        missing = POST_FIELDS - set(post)
        extra = set(post) - POST_FIELDS
        raise ValueError(
            "Post JSON fields mismatch (missing: {}; extra: {}).".format(
                ", ".join(sorted(missing)) or "none",
                ", ".join(sorted(extra)) or "none",
            )
        )
    if post["aida_stage"] != stage:
        raise ValueError(
            "Expected aida_stage {!r}, received {!r}.".format(
                stage, post["aida_stage"]
            )
        )
    for field in POST_FIELDS - {"hashtags"}:
        if not isinstance(post[field], str) or not post[field].strip():
            raise ValueError("{} must be a non-empty string.".format(field))
    if len(post["short_version"]) >= 280:
        raise ValueError("short_version must be fewer than 280 characters.")
    if not 600 <= len(post["long_version"]) <= 1200:
        raise ValueError("long_version must be between 600 and 1200 characters.")
    hashtags = post["hashtags"]
    if (
        not isinstance(hashtags, list)
        or len(hashtags) > 2
        or any(not isinstance(tag, str) or not tag.strip() for tag in hashtags)
    ):
        raise ValueError("hashtags must be an array of zero to two non-empty strings.")
    post_copy = "{} {}".format(post["short_version"], post["long_version"]).casefold()
    all_content = " ".join(
        [value for value in post.values() if isinstance(value, str)]
        + post["hashtags"]
    ).casefold()
    for banned in ("revolutionary", "groundbreaking", "game-changing"):
        if banned in all_content:
            raise ValueError("Generated post contains disallowed hype: {}.".format(banned))
    if stage == "awareness" and "r2c.ai" in all_content:
        raise ValueError("Awareness posts must not mention R2C.AI.")
    if stage == "action" and "r2c.ai" not in post_copy:
        raise ValueError("Action posts must mention R2C.AI.")
    if stage == "action" and not any(
        phrase in post_copy
        for phrase in (
            "review one",
            "upload one",
            "explore one",
            "map one",
            "share one",
            "submit one",
            "complete one",
        )
    ):
        raise ValueError(
            "Action posts must include a specific, measurable one-item next step."
        )
    image_prompt = post["image_prompt"].strip()
    if not 500 <= len(image_prompt) <= 2400:
        raise ValueError(
            "image_prompt must be a detailed 500-2400 character visual brief."
        )
    return post


def generate_post_record(
    trend: Dict[str, Any], stage: str, gemini_client: Any
) -> Dict[str, Any]:
    angle = STAGE_CREATIVE_ANGLE.get(stage, "Evidence-led insight")
    result: Dict[str, Any] = {
        "trend_id": trend.get("trend_id"),
        "rank": trend.get("rank"),
        "topic_title": trend.get("topic_title"),
        "aida_stage": stage,
        "creative_angle": angle,
        "aspect_ratio": config.AD_STAGE_ASPECT_RATIO.get(stage),
        "prompt_version": PROMPT_VERSION,
        "post": None,
        "status": "failed",
        "error_message": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        response_text = gemini_client.generate_text(
            make_post_request(trend, stage, angle),
            label="generate_{}_social_post".format(stage),
        )
        if not response_text:
            raise RuntimeError("Gemini returned an empty social post.")
        result["post"] = parse_and_validate_post(response_text, stage)
        result["status"] = "success"
    except (RuntimeError, ValueError, TypeError) as exc:
        result["error_message"] = "{}: {}".format(type(exc).__name__, exc)
        logger.exception(
            "Could not generate %s social post for trend %s",
            stage,
            trend.get("trend_id"),
        )
    return result


def generate_prompt_record(trend: Dict[str, Any], gemini_client: Any) -> Dict[str, Any]:
    """Compatibility wrapper: generate the awareness-stage post record."""
    return generate_post_record(trend, "awareness", gemini_client)


def write_results(path: str, results: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(results, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def run(
    input_path: str = config.RANKED_TRENDS_JSON,
    output_path: str = config.GENERATED_PROMPTS_JSON,
    gemini_client: Optional[Any] = None,
    stage: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not config.GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is required to generate social content.")
    trends = load_trends(input_path)
    stages = [stage] if stage else list(AIDA_STAGES)
    for item in stages:
        _stage_template(item)
    client = gemini_client if gemini_client is not None else get_client()
    if not client.online:
        raise RuntimeError("Gemini is not available for social-content generation.")

    results: List[Dict[str, Any]] = []
    for trend in trends:
        for item in stages:
            results.append(generate_post_record(trend, item, client))
            write_results(output_path, results)
    successes = sum(item["status"] == "success" for item in results)
    logger.info(
        "AIDA post generation finished: %d succeeded, %d failed; output: %s",
        successes,
        len(results) - successes,
        output_path,
    )
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate AIDA social posts and image briefs from ranked trends."
    )
    parser.add_argument("--input", default=config.RANKED_TRENDS_JSON)
    parser.add_argument("--output", default=config.GENERATED_PROMPTS_JSON)
    parser.add_argument("--stage", choices=AIDA_STAGES, help="Generate one AIDA stage only.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format=config.LOG_FORMAT,
        stream=sys.stdout,
    )
    try:
        results = run(args.input, args.output, stage=args.stage)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        logger.error("Social-content generation could not start: %s", exc)
        return 2
    return 0 if all(item["status"] == "success" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
