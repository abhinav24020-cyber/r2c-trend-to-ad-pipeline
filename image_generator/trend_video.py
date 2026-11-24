"""
Trend-specific video ad: a 25-35 second vertical (9:16) video about ONE trend.

    script   Gemini writes 4 scenes (hook, proof, bridge, call to action) using only the trend's crawled
             facts and sources; an automatic check rejects copy that could fit another trend. If Gemini is
             down, Cloudflare's gpt-oss-120b writes it instead.
    pictures 3 photoreal stills from Cloudflare Workers AI: FLUX.2 Klein 9B for the main scene, Klein 4B for
             scenes 2-3 (about 1,570 of the 10,000 free daily neurons per video).
    voice    edge-tts neural voice with word timings (for the karaoke captions).
    music    original music and transition effects synthesised with numpy (no licence issues).
    graphics trend_video_overlay.html animated frame by frame in headless Chrome (Playwright): logo, trend
             tag, source credit, kinetic headline, captions, R2C.Ai feature chips, brand wipes, end card.
    video    camera moves over the stills (pan, push in, rise), graphics on top, voice over ducked music,
             encoded to H.264 / AAC with the bundled ffmpeg.

    from image_generator import trend_video
    result = trend_video.make_trend_video(trend, "static/daily/<id>", gemini_client)
    result["video"]  -> static/daily/<id>/trend_video.mp4

Extra packages (requirements-video.txt): numpy, scipy, edge-tts, playwright (+ a Chrome/Chromium).
"""
from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import wave
from typing import Any, Dict, List, Optional, Sequence

from trend_pipeline import config

logger = logging.getLogger("trend_video")

HERE = os.path.dirname(os.path.abspath(__file__))
OVERLAY_HTML = os.path.join(HERE, "trend_video_overlay.html")
LOGO = os.path.join(os.path.dirname(HERE), "static", "img", "r2c_logo_white.png")
W, H = 1080, 1920            # render size (the overlay is laid out for it)
OUT_W, OUT_H = 720, 1280     # published size: sharp on phones, small enough for the site
FPS, SR = 30, 44100
VOICE = os.environ.get("TREND_VIDEO_VOICE", "en-US-AndrewMultilingualNeural")
MAIN_MODEL = "@cf/black-forest-labs/flux-2-klein-9b"
SCENE_MODEL = "@cf/black-forest-labs/flux-2-klein-4b"
JARGON = ("latent", "logits", "kv cache", "embedding", "gradient")
_SKIP_WORDS = {"The", "This", "These", "Researchers", "Recent", "Webpage", "Hugging", "Face", "Hacker", "News",
               "Prompting", "Logit", "Text", "Additionally", "Bluesky", "Mastodon", "GitHub", "Reddit", "However"}
_GENERIC_NAMES = {"AI", "ML", "VLM", "IIIT", "R2C", "LLM", "LLMs", "RL", "API", "GPU"}


# --------------------------------------------------------------------------- script
def build_prompt(trend: Dict[str, Any]) -> str:
    from prompt_generator.ad_creatives import ALLOWED_CTAS, BENEFITS, BRAND

    sources = [{"title": s.get("source_insight"), "platform": s.get("platform"), "url": s.get("source_url"),
                "excerpt": (s.get("excerpt") or "")[:400]} for s in (trend.get("source_attribution") or [])[:6]]
    return f"""You write TREND-SPECIFIC 25-second vertical video ads. The viewer must learn something real and new about THIS trend; a generic ad that could fit any AI news is a failure.

TREND: {trend.get('topic_title')}
SUMMARY: {trend.get('topic_summary')}
KEY FACTS (the only facts you may state): {json.dumps(trend.get('topic_key_facts') or [], ensure_ascii=False)}
SOURCES: {json.dumps(sources, ensure_ascii=False)}
BRAND (only claim these): {BRAND['name']} - {BRAND['about']} For researchers: {BENEFITS['researchers']}. Allowed CTAs: {ALLOWED_CTAS}

Write 4 scenes:
1 HOOK (10-12 spoken words): the single most striking concrete result, naming the system/method/benchmark.
2 PROOF (13-16 words): a second named system/result from the facts, concrete numbers or capabilities only if in the facts.
3 BRIDGE (14-17 words): why this exact kind of work is valuable to industry, and that R2C.Ai at IIIT Delhi helps researchers in it reach industry partners.
4 CTA (9-12 words): invite researchers working on THIS topic, plus R2C.Ai.
EVERY scene must be about THIS trend: scene 3 must name one of the systems/methods from scenes 1-2 again (e.g. "work like INSEP"), and scene 4 must name the concrete research topic (e.g. "model introspection", "agent harnesses"), never just a broad field like "AI" or "LLM research". Nothing in the ad may fit a different trend.
Rules: only facts above, no invented numbers, no hype. PLAIN LANGUAGE: name the system, then say what it does in words a LinkedIn/Instagram viewer understands; avoid jargon (latent, logits, KV cache, embeddings, RL, on-policy, gradient) unless it is in the trend title or you explain it in plain words. "cta" must be EXACTLY one of the allowed CTAs, copied verbatim. Scene 3 voiceover must say R2C.Ai at IIIT Delhi connects researchers in this area with industry partners. Also return "audience_line": max 7 words naming who the ad is for (e.g. "For LLM safety researchers"), "tag": max 3 words naming the trend (e.g. "LLM SAFETY"), and "chips": 3 R2C.Ai benefits (max 4 words each) taken from the researcher list. on_screen max 6 words and must contain a named system or a concrete term from the trend in scenes 1-3. highlight_word: one word of on_screen to highlight. Each scene has "fact_used": the exact key fact or source title it relies on.
image_prompt (scenes 1-3): a cinematic photoreal still that makes this topic visible with PEOPLE and a strong visual metaphor (e.g. Indian researchers in a modern lab, a glowing physical model of the idea, a whiteboard of shapes without writing). No text, letters, logos or readable screens. Navy / royal blue / amber palette, vertical framing, each scene a different setting. motion: one sentence describing the movement.
"source_line": short on-screen credit like "Source: arXiv, Nov 2026". "end_line": max 5 words, a call to the researchers that names this trend's concrete topic or a named system from it.
Return JSON: {{"scenes":[{{"role","voiceover","on_screen","highlight_word","fact_used","image_prompt","motion"}}],"cta","source_line","end_line","audience_line","tag","chips":[str]}}"""


def trend_terms(trend: Dict[str, Any]) -> set:
    """Names and figures that only this trend has: model/method names (INSEP, MatHarness, Qwen) and exact figures."""
    facts = " ".join(trend.get("topic_key_facts") or [])
    corpus = " ".join([facts, trend.get("topic_summary") or "", trend.get("topic_title") or ""] +
                      [s.get("source_insight") or "" for s in trend.get("source_attribution") or []])
    names = set(re.findall(r"\b[A-Z][A-Za-z0-9\-]*[A-Z0-9][A-Za-z0-9\-]*\b", corpus)) - _GENERIC_NAMES
    names |= {w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", facts) if w not in _SKIP_WORDS}
    names |= set(re.findall(r"\d+(?:\.\d+)?%", facts))
    return names


def check_script(script: Dict[str, Any], trend: Dict[str, Any]) -> Dict[str, Any]:
    """The automatic trend-specific check. pass=False means the copy could fit another trend or breaks a rule."""
    from prompt_generator.ad_creatives import ALLOWED_CTAS

    scenes = script.get("scenes") or []
    names = trend_terms(trend)
    title_words = [w.lower() for w in re.findall(r"[A-Za-z]{5,}", trend.get("topic_title") or "")]
    ad_text = " ".join((s.get("voiceover") or "") + " " + (s.get("on_screen") or "") for s in scenes)
    used = sorted(n for n in names if n in ad_text)
    end_line = script.get("end_line") or ""
    result = {
        "named_terms_used": used,
        "scenes_without_trend_terms": [s.get("role") for s in scenes[:3]
                                       if not any(n in (s.get("voiceover") or "") + (s.get("on_screen") or "") for n in names)],
        "end_specific": any(n in end_line for n in names) or any(w in end_line.lower() for w in title_words),
        "cta_ok": script.get("cta") in ALLOWED_CTAS,
        "shape_ok": len(scenes) == 4 and len(script.get("chips") or []) == 3
                    and all(s.get("voiceover") and s.get("on_screen") for s in scenes),
        "jargon": [j for j in JARGON if j in ad_text.lower() and j not in (trend.get("topic_title") or "").lower()],
    }
    result["pass"] = (len(used) >= 3 and not result["scenes_without_trend_terms"] and result["end_specific"]
                      and result["cta_ok"] and result["shape_ok"] and not result["jargon"])
    return result


def _parse_json(text: str) -> Dict[str, Any]:
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0] or [0])
    data = json.loads(text[start: max(text.rfind("}"), text.rfind("]")) + 1])
    return data[0] if isinstance(data, list) else data


def _cloudflare_text(prompt: str) -> str:
    import requests

    url = "https://api.cloudflare.com/client/v4/accounts/{}/ai/run/@cf/openai/gpt-oss-120b".format(
        config.CLOUDFLARE_ACCOUNT_ID)
    response = requests.post(url, headers={"Authorization": "Bearer " + config.CLOUDFLARE_API_TOKEN}, json={
        "input": [{"role": "system", "content": "Reply with one JSON object only, no markdown."},
                  {"role": "user", "content": prompt}],
        "reasoning": {"effort": "medium"}}, timeout=240)
    if response.status_code != 200:
        raise RuntimeError("Cloudflare text {}: {}".format(response.status_code, response.text[:200]))
    out = response.json()["result"]["output"]
    return [c["text"] for o in out for c in (o.get("content") or []) if c.get("type") == "output_text"][-1]


def write_script(trend: Dict[str, Any], gemini: Any, attempts: int = 3) -> Dict[str, Any]:
    """Best script over a few attempts; the first one that passes the check wins. Raises if none is usable.
    Writer: gpt-oss-120b on Cloudflare (as in the approved sample) for the first two attempts, Gemini after that
    or whenever Cloudflare is unavailable."""
    prompt = build_prompt(trend)
    best: Optional[Dict[str, Any]] = None
    for attempt in range(attempts):
        text, writer = None, "gpt-oss-120b (Cloudflare)"
        if attempt < 2 and config.CLOUDFLARE_ACCOUNT_ID and config.CLOUDFLARE_API_TOKEN:
            try:
                text = _cloudflare_text(prompt)
            except Exception as exc:  # noqa: BLE001
                logger.warning("gpt-oss-120b unavailable: %s", str(exc)[:200])
        if not text and getattr(gemini, "online", False):
            writer = "gemini"
            text = gemini.generate_text(prompt, label="trend_video_script", json_mode=True)
        if not text:
            continue
        try:
            script = _parse_json(text)
        except ValueError:
            logger.warning("Script attempt %d: answer was not JSON", attempt + 1)
            continue
        script["writer"] = writer
        script["check"] = check_script(script, trend)
        logger.info("Script attempt %d (%s): %s", attempt + 1, writer, script["check"])
        if script["check"]["shape_ok"] and (best is None or script["check"]["pass"]
                                            or len(script["check"]["named_terms_used"]) > len(best["check"]["named_terms_used"])):
            best = script
        if script["check"]["pass"]:
            break
    if best is None:
        raise RuntimeError("no usable video script")
    return best


# --------------------------------------------------------------------------- pictures
def make_pictures(script: Dict[str, Any], folder: str) -> List[str]:
    """The 3 scene pictures with FLUX.2 Klein 9B (Klein 4B only if 9B fails for a reason other than the daily
    limit). About 1,364 neurons each, so 2 videos fit in Cloudflare's free 10,000 a day. Returns the file paths."""
    from image_generator import generate_images, picture_quality

    width, height = config.AD_ASPECT_SIZES["9:16"]
    paths = []
    for i, scene in enumerate(script["scenes"][:3]):
        prompt = ((scene.get("image_prompt") or "") + " Cinematic photoreal film still, shallow depth of field, anamorphic "
                  "lighting, rich detail, vertical 9:16 composition with the subject in the middle third and calm space at "
                  "top and bottom. No text, no letters, no logos, no watermarks.")
        image = None
        for model in (MAIN_MODEL, SCENE_MODEL):
            client = generate_images.CloudflareImageClient(config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN,
                                                           model, config.CLOUDFLARE_IMAGE_STEPS)
            try:
                image = client.text_to_image(prompt=prompt, width=width, height=height,
                                             negative_prompt=generate_images.NEGATIVE_FOR_BACKGROUND)
                break
            except (RuntimeError, OSError) as exc:
                logger.warning("Scene %d picture with %s failed: %s", i + 1, model.split("/")[-1], str(exc)[:160])
                if "429" in str(exc) or "allocation" in str(exc).lower():
                    raise
        if image is None:
            raise RuntimeError("scene {} picture failed".format(i + 1))
        path = os.path.join(folder, "scene{}.jpg".format(i + 1))
        picture_quality.polish(image).convert("RGB").save(path, "JPEG", quality=93)
        paths.append(path)
    return paths


# --------------------------------------------------------------------------- motion
LTX_SPACE = "Lightricks/ltx-video-distilled"


def make_motion(script: Dict[str, Any], pictures: Sequence[str], folder: str) -> List[Optional[str]]:
    """LTX-Video (distilled) image-to-video on the free Hugging Face demo: one ~4 s clip per scene picture.
    The free demo allows only a few clips a day; a scene without a clip gets a camera move instead."""
    clips: List[Optional[str]] = [None] * len(pictures)
    if os.environ.get("TREND_VIDEO_MOTION", "1") == "0":
        return clips
    try:
        from gradio_client import Client, handle_file
    except ImportError:
        return clips
    client = None
    for token in ([os.environ["HF_TOKEN"]] if os.environ.get("HF_TOKEN") else []) + [None]:
        try:  # a Hugging Face login gets more free GPU time than an anonymous visitor
            client = Client(LTX_SPACE, hf_token=token, verbose=False) if token else Client(LTX_SPACE, verbose=False)
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("LTX-Video demo unavailable%s: %s", " with HF_TOKEN" if token else "", str(exc)[:200])
    if client is None:
        return clips
    for i, picture in enumerate(pictures):
        scene = script["scenes"][i]
        started = time.monotonic()
        try:
            result, _seed = client.predict(
                prompt=(scene.get("motion") or "Slow cinematic camera push in") +
                " Smooth cinematic camera motion, realistic physics, stable subject, high detail.",
                negative_prompt="worst quality, inconsistent motion, blurry, jittery, distorted, morphing, text, watermark",
                input_image_filepath=handle_file(picture), input_video_filepath=None,
                height_ui=1024, width_ui=576, mode="image-to-video", duration_ui=4, ui_frames_to_use=9,
                seed_ui=42, randomize_seed=False, ui_guidance_scale=1, improve_texture_flag=True,
                api_name="/image_to_video")
            path = os.path.join(folder, "clip{}.mp4".format(i + 1))
            shutil.copyfile(result["video"], path)
            clips[i] = path
            logger.info("Scene %d motion clip in %.0fs", i + 1, time.monotonic() - started)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Scene %d motion failed (camera move instead): %s", i + 1, str(exc)[:200])
            if "quota" in str(exc).lower() or "exceeded" in str(exc).lower():
                break  # the free daily GPU time is used up: the remaining scenes get camera moves
    return clips


def clip_frames(clip: str, seconds: float):
    """The clip slowed down (motion-interpolated, at most 1.6x) at 30 fps, as small RGB frames; render_video
    plays them forward then backward if the scene is longer than the clip."""
    import numpy as np

    probe = subprocess.run([_ffmpeg(), "-nostdin", "-i", clip], capture_output=True, text=True).stderr
    match = re.search(r"Duration: (\d+):(\d+):([\d.]+)", probe)
    length = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3)) if match else 4.0
    slow = min(1.6, max(1.0, seconds / max(0.5, length)))
    raw = subprocess.run([_ffmpeg(), "-nostdin", "-loglevel", "error", "-i", clip, "-vf",
                          "setpts={:.3f}*PTS,minterpolate=fps={}:mi_mode=mci,scale=576:1024".format(slow, FPS),
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.uint8).reshape(-1, 1024, 576, 3)


def ping_pong(k: int, count: int) -> int:
    """Frame index k of a clip with `count` frames played forward, then backward, and so on."""
    if count <= 1:
        return 0
    period = 2 * count - 2
    k %= period
    return k if k < count else period - k


# --------------------------------------------------------------------------- voice
async def _speak(text: str, path: str) -> List[Dict[str, float]]:
    import edge_tts

    words, audio = [], bytearray()
    async for chunk in edge_tts.Communicate(text, VOICE, rate="+4%", boundary="WordBoundary").stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
        elif chunk["type"] == "WordBoundary":
            words.append({"w": chunk["text"], "t": chunk["offset"] / 1e7, "d": chunk["duration"] / 1e7})
    if not audio or not words:
        raise RuntimeError("no audio from the voice service")
    with open(path, "wb") as handle:
        handle.write(audio)
    return words


def make_voice(script: Dict[str, Any], folder: str) -> List[List[Dict[str, float]]]:
    """One voice file per scene (vo1..vo4.mp3) and the word timings for the captions."""
    all_words = []
    for i, scene in enumerate(script["scenes"], 1):
        for attempt in range(8):  # the free voice service sometimes returns nothing; it works on a retry
            try:
                all_words.append(asyncio.run(_speak(scene["voiceover"], os.path.join(folder, "vo{}.mp3".format(i)))))
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 7:
                    raise RuntimeError("voice failed for scene {}: {}".format(i, exc))
                time.sleep(3 + attempt * 2)
    return all_words


def timeline(words: Sequence[Sequence[Dict[str, float]]]) -> Dict[str, Any]:
    """Scene lengths follow the voice: each scene = its voice + 0.6 s, the last one + 1.6 s for the end card."""
    voice = [w[-1]["t"] + w[-1]["d"] for w in words]
    durs = [v + 0.6 for v in voice]
    durs[-1] = voice[-1] + 1.6
    starts = [round(sum(durs[:i]), 4) for i in range(len(durs))]
    return {"starts": starts, "durs": durs, "total": sum(durs), "voice_offset": 0.25}


# --------------------------------------------------------------------------- music
def make_music(tl: Dict[str, Any], path: str) -> None:
    """Original underscore: pads + bass + arpeggio, a tense pulse in scene 2, drums from scene 3, whooshes at the
    cuts, a riser into the call to action and impacts. Deterministic (seeded)."""
    import numpy as np
    from scipy.signal import lfilter

    starts, total = tl["starts"], tl["total"]
    n_total = int((total + 0.2) * SR)
    left, right = np.zeros(n_total), np.zeros(n_total)
    rng = np.random.default_rng(7)

    def note(m):
        return 440 * 2 ** ((m - 69) / 12)

    def lp(x, a):
        return lfilter([a], [1, a - 1], x)

    def add(sig, start, gain=1.0, pan=0.0):
        i = int(start * SR)
        j = min(n_total, i + len(sig))
        if j <= i or i < 0:
            return
        left[i:j] += sig[:j - i] * gain * (1 - pan)
        right[i:j] += sig[:j - i] * gain * (1 + pan)

    def section(x):
        return sum(x >= s for s in starts) - 1

    beat = 60 / 96
    bar = beat * 4
    chords = [[57, 60, 64], [53, 57, 60], [48, 52, 55, 60], [55, 59, 62]]  # Am F C G
    for b in range(int(total / bar) + 2):
        t0 = b * bar
        chord, sc = chords[b % 4], section(t0)
        n = int(bar * SR) + int(0.6 * SR)
        tt = np.arange(n) / SR
        env = np.minimum(1, tt / 0.35) * np.exp(-np.maximum(0, tt - bar) * 6)
        pad = np.zeros(n)
        for m in chord + [chord[0] + 12]:
            for det in (-0.12, 0, 0.12):
                ph = 2 * np.pi * note(m + det) * tt
                pad += (2 * ((ph / (2 * np.pi)) % 1) - 1) * 0.5 + np.sin(ph) * 0.5
        cut = 0.03 if sc == 1 else 0.06 if sc == 0 else 0.11
        pad = lp(lp(pad * env, cut), cut) * 0.035
        add(pad, t0, 1, -0.2)
        add(np.roll(pad, 600), t0, 1, 0.2)
        for k in (0, 2):
            bt = np.arange(int(beat * 2 * SR)) / SR
            add(np.sin(2 * np.pi * note(chord[0] - 24) * bt) * np.exp(-bt * 2.2) * 0.22, t0 + k * beat)
        if sc != 1:
            arp = chord + [chord[0] + 12]
            for k in range(8):
                at = np.arange(int(0.45 * SR)) / SR
                f = note(arp[k % len(arp)] + 12)
                pluck = (np.sin(2 * np.pi * f * at) + 0.3 * np.sin(4 * np.pi * f * at)) * np.exp(-at * 9)
                add(pluck * (0.07 if sc == 0 else 0.09), t0 + k * beat / 2, 1, 0.35 if k % 2 else -0.35)
        else:
            for k in range(4):
                bt = np.arange(int(0.3 * SR)) / SR
                add(np.sin(2 * np.pi * (55 * np.exp(-bt * 8) + 40) * bt) * np.exp(-bt * 14) * 0.35, t0 + k * beat)
        if sc >= 2:
            for k in range(4):
                kt = np.arange(int(0.35 * SR)) / SR
                add(np.sin(2 * np.pi * np.cumsum(50 + 110 * np.exp(-kt * 30)) / SR) * np.exp(-kt * 9) * 0.45, t0 + k * beat)
                hh = rng.standard_normal(int(0.06 * SR))
                hh = (hh - lp(hh, 0.5)) * np.exp(-np.arange(len(hh)) / SR * 70)
                add(hh * 0.05, t0 + k * beat + beat / 2, 1, 0.3)
                if k in (1, 3):
                    sn = rng.standard_normal(int(0.2 * SR)) * np.exp(-np.arange(int(0.2 * SR)) / SR * 22)
                    add(lp(sn, 0.35) * 0.12, t0 + k * beat)

    def whoosh(at, length=0.55, gain=0.16):
        n = int(length * SR)
        e = np.sin(np.linspace(0, np.pi, n)) ** 2
        y = lp(rng.standard_normal(n), 0.12)
        add(y * e * gain, at - length * 0.6, 1, -0.3)
        add(y[::-1] * e * gain * 0.6, at - length * 0.6, 1, 0.3)

    def impact(at, gain=0.5):
        n = int(1.6 * SR)
        it = np.arange(n) / SR
        boom = np.sin(2 * np.pi * np.cumsum(35 + 60 * np.exp(-it * 12)) / SR) * np.exp(-it * 2.6)
        add(boom * gain + lp(rng.standard_normal(n), 0.08) * np.exp(-it * 7) * gain * 0.6, at)

    for s in starts[1:]:
        whoosh(s)
    last = starts[-1]
    rn = int(2.2 * SR)
    rt = np.arange(rn) / SR
    riser = lp(rng.standard_normal(rn), 0.02) * (rt / rt[-1]) ** 2.5
    riser += np.sin(2 * np.pi * np.cumsum(200 + 900 * (rt / rt[-1]) ** 2) / SR) * (rt / rt[-1]) ** 3 * 0.15
    add(riser * 0.5, last - 2.2)
    impact(0.0, 0.35)
    impact(last, 0.6)
    mix = np.stack([left, right], 1)
    fade = np.ones(n_total)
    fl = int(1.8 * SR)
    fade[-fl:] = np.linspace(1, 0, fl) ** 1.5
    mix *= fade[:, None]
    mix /= max(1e-9, np.abs(mix).max() / 0.89)
    _write_wav(path, mix)


def _write_wav(path: str, stereo) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SR)
        handle.writeframes((stereo * 32767).astype("<i2").tobytes())


def _ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def mix_audio(folder: str, tl: Dict[str, Any], music_path: str, out_path: str) -> None:
    """Voice on top; the music ducks under it (fast attack, slow release)."""
    import numpy as np

    def decode(path):
        raw = subprocess.run([_ffmpeg(), "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "2",
                              "-ar", str(SR), "-"], capture_output=True, check=True).stdout
        return np.frombuffer(raw, "<i2").reshape(-1, 2).astype(np.float32) / 32768

    music = decode(music_path)
    n = len(music)
    voice = np.zeros_like(music)
    for i, start in enumerate(tl["starts"]):
        v = decode(os.path.join(folder, "vo{}.mp3".format(i + 1)))
        a = int((start + tl["voice_offset"]) * SR)
        b = min(n, a + len(v))
        voice[a:b] += v[:b - a]
    env = np.abs(voice).max(1)
    blk = 44
    peaks = env[: n // blk * blk].reshape(-1, blk).max(1)
    follow = np.empty_like(peaks)
    acc = 0.0
    for k, x in enumerate(peaks):
        acc += (x - acc) * (0.6 if x > acc else 0.004)
        follow[k] = acc
    follow = np.repeat(follow, blk)
    follow = np.pad(follow, (0, n - len(follow)), "edge")
    duck = 1 - 0.72 * np.clip(follow / 0.12, 0, 1)
    mix = voice * 1.15 + music * (0.42 * duck)[:, None]
    mix /= max(1e-6, np.abs(mix).max() / 0.93)
    _write_wav(out_path, mix)


# --------------------------------------------------------------------------- graphics + video
def _push(img, z, dx=0.0, dy=0.0):
    """Scale about the centre by z with a sub-pixel offset (smooth camera move)."""
    from PIL import Image

    w, h = img.size
    cx, cy = w / 2 + dx, h / 2 + dy
    return img.transform((w, h), Image.AFFINE, (1 / z, 0, cx - w / (2 * z), 0, 1 / z, cy - h / (2 * z)), Image.BICUBIC)


def _ease(x):
    return x * x * (3 - 2 * x)


# pan across, push in, rise up; the end card drifts over the first picture
MOVES = [lambda p: (1.18 - 0.06 * p, -90 + 180 * _ease(p), 0),
         lambda p: (1.02 + 0.16 * _ease(p), 0, 30 * p),
         lambda p: (1.14, 0, 110 - 220 * _ease(p)),
         lambda p: (1.04 + 0.10 * p, 0, -40 * p)]


def overlay_data(script: Dict[str, Any], tl: Dict[str, Any], words) -> Dict[str, Any]:
    esc = html.escape
    v3 = words[2][-1]["t"] + words[2][-1]["d"]
    chip_at = [tl["starts"][2] + tl["voice_offset"] + v3 * f for f in (0.45, 0.62, 0.79)]
    scenes = [{"on_screen": esc(s["on_screen"]), "highlight_word": s.get("highlight_word") or ""} for s in script["scenes"]]
    return {**tl, "scenes": scenes, "words": [[{**w, "w": esc(w["w"])} for w in ws] for ws in words],
            "cta": esc(script["cta"]), "url": "r2c.iiitd.edu.in", "logo": pathlib.Path(LOGO).as_uri(),
            "chips": [{"text": esc(c), "at": a} for c, a in zip(script["chips"], chip_at)],
            "tag": esc(script.get("tag") or "AI RESEARCH"), "source": esc(script.get("source_line") or ""),
            "end_line": esc(script.get("end_line") or "Upload your research."),
            "audience": esc(script.get("audience_line") or "For AI researchers")}


def _launch(playwright):
    try:
        return playwright.chromium.launch()
    except Exception:  # noqa: BLE001 - no bundled Chromium (e.g. a laptop): use the installed Chrome
        return playwright.chromium.launch(channel="chrome")


def render_video(script, tl, words, pictures: Sequence[str], audio_path: str, out_path: str,
                 clips: Optional[Sequence[Optional[str]]] = None) -> None:
    """Graphics frames from the overlay page, composited over the motion clips (or camera moves over the
    pictures), piped straight into ffmpeg."""
    from PIL import Image
    from playwright.sync_api import sync_playwright

    stills = [Image.open(p).convert("RGB").resize((W, H), Image.LANCZOS) for p in pictures]
    stills.append(stills[0])  # end card background
    starts, durs = tl["starts"], tl["durs"]
    clips = list(clips or [])
    loaded = {"scene": None, "frames": None}

    def background(si: int, lt: float):
        """Scene si at local time lt: its motion clip (slowed, ping-pong) with a gentle push, else a camera move."""
        if si < 3 and si < len(clips) and clips[si]:
            if loaded["scene"] != si:  # decode one scene's clip at a time (keeps memory small)
                try:
                    loaded["frames"] = clip_frames(clips[si], durs[si])
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Scene %d clip unreadable, camera move instead: %s", si + 1, exc)
                    loaded["frames"] = None
                    clips[si] = None
                loaded["scene"] = si
            frames = loaded["frames"]
            if frames is not None and len(frames):
                img = Image.fromarray(frames[ping_pong(int(lt * FPS), len(frames))]).resize((W, H), Image.BICUBIC)
                return _push(img, 1.0 + 0.06 * lt / durs[si])
        z, dx, dy = MOVES[min(si, 3)](lt / durs[si])
        return _push(stills[min(si, 3)], z, dx, dy)

    nframes = int(tl["total"] * FPS)
    encoder = subprocess.Popen(
        [_ffmpeg(), "-nostdin", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "{}x{}".format(W, H),
         "-r", str(FPS), "-i", "-", "-i", audio_path, "-vf", "scale={}:{}:flags=lanczos".format(OUT_W, OUT_H),
         "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
         "-shortest", "-movflags", "+faststart", out_path], stdin=subprocess.PIPE)
    try:
        with sync_playwright() as p:
            browser = _launch(p)
            page = browser.new_page(viewport={"width": W, "height": H})
            page.goto(pathlib.Path(OVERLAY_HTML).as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("document.fonts.ready")
            page.evaluate("setup({})".format(json.dumps(overlay_data(script, tl, words))))
            page.wait_for_timeout(500)
            for n in range(nframes):
                t = n / FPS
                si = max(i for i in range(len(starts)) if t >= starts[i])
                frame = background(si, t - starts[si]).convert("RGBA")
                page.evaluate("render({})".format(t))
                overlay = Image.open(io.BytesIO(page.screenshot(omit_background=True))).convert("RGBA")
                frame.alpha_composite(overlay)
                encoder.stdin.write(frame.convert("RGB").tobytes())
            browser.close()
    finally:
        encoder.stdin.close()
        encoder.wait()
    if encoder.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("ffmpeg could not encode the video")


# --------------------------------------------------------------------------- all steps
def make_trend_video(trend: Dict[str, Any], folder: str, gemini: Any, script: Optional[Dict[str, Any]] = None
                     ) -> Dict[str, Any]:
    """Script -> pictures -> motion -> voice -> music -> graphics -> folder/trend_video.mp4. Raises on failure.
    The main scene picture is kept as folder/scene1.jpg (the ad photo is made from it)."""
    from trend_pipeline.web_export import slim_trend

    if "composite_trend_score" in trend:  # a ranked-trends record: use the site's slim form (facts, sources)
        trend = slim_trend(trend)
    started = time.monotonic()
    os.makedirs(folder, exist_ok=True)
    work = tempfile.mkdtemp(prefix="trend_video_")
    try:
        script = script or write_script(trend, gemini)
        pictures = make_pictures(script, work)
        shutil.copyfile(pictures[0], os.path.join(folder, "scene1.jpg"))
        clips = make_motion(script, pictures, work)
        words = make_voice(script, work)
        tl = timeline(words)
        make_music(tl, os.path.join(work, "music.wav"))
        mix_audio(work, tl, os.path.join(work, "music.wav"), os.path.join(work, "audio.wav"))
        out = os.path.join(folder, "trend_video.mp4")
        render_video(script, tl, words, pictures, os.path.join(work, "audio.wav"), out, clips)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    seconds = round(time.monotonic() - started)
    logger.info("Trend video done in %ds (%.1f s long, %.1f MB, %d motion clips): %s", seconds, tl["total"],
                os.path.getsize(out) / 1e6, sum(1 for c in clips if c), trend.get("topic_title"))
    return {"video": out, "script": script, "seconds": seconds, "duration": round(tl["total"], 1),
            "picture": os.path.join(folder, "scene1.jpg"), "motion_clips": sum(1 for c in clips if c)}
