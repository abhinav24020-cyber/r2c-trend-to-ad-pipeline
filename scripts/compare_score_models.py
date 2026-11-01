"""Gemini vision scores each model's raw picture against its prompt; writes scores.json in samples/."""
import json, os, re, sys
sys.path.insert(0, os.getcwd())
from PIL import Image
from trend_pipeline.gemini_client import get_client

SAMPLES = os.path.join("data", "output", "samples")
FOLDERS = ["flux-2-klein-9b-v2", "stable-diffusion-xl-base-1.0", "stable-diffusion-3.5-medium", "20261029_1352"]
OUT = os.path.join(SAMPLES, "model_scores.json")
RUBRIC = """You are judging an AI-generated picture for a social media ad. The picture was generated from the PROMPT below.
Score each criterion from 1 (very poor) to 10 (excellent):
- adherence: how faithfully the picture shows the subject, action, setting and colour palette in the prompt
- realism: how much it looks like a real photo or a professionally designed illustration, NOT obviously AI-generated (plastic skin, warped hands/faces, melted objects lower this)
- catchiness: would it stop someone scrolling Instagram or LinkedIn
- artifacts: 10 = no visible defects; lower for gibberish text, extra fingers, distorted faces, broken objects
Reply with JSON only: {"adherence": n, "realism": n, "catchiness": n, "artifacts": n, "note": "one short sentence"}

PROMPT:
"""

gemini = get_client()
scores = json.load(open(OUT, encoding="utf-8")) if os.path.isfile(OUT) else {}
for folder in FOLDERS:
    path = os.path.join(SAMPLES, folder, "samples.json")
    if not os.path.isfile(path):
        continue
    for item in json.load(open(path, encoding="utf-8")):
        res = item.get("result") or {}
        key = "{}|{}".format(folder, item["record"]["trend_id"])
        if key in scores or res.get("status") != "success":
            continue
        if os.path.isfile(res.get("image_path") or ""):
            img = Image.open(res["image_path"]).convert("RGB")
        else:  # raw picture not kept: crop the picture area out of the finished (dark-template) ad
            from image_generator.compose_ads import LAYOUTS
            ad = Image.open(res["ad_path"]).convert("RGB")
            x0, y0, x1, y1 = LAYOUTS[item["record"]["aspect_ratio"]]["picture"]
            img = ad.crop((int(ad.width * x0), int(ad.height * y0), int(ad.width * x1), int(ad.height * y1)))
        img.thumbnail((1024, 1024))
        prompt = RUBRIC + item["record"]["image_prompt"]
        raw = gemini._client.models.generate_content(
            model=__import__("trend_pipeline.config", fromlist=["x"]).GEMINI_TEXT_MODEL,
            contents=[img, prompt], config={"response_mime_type": "application/json"}).text
        gemini._pace()
        try:
            data = json.loads(re.sub(r"^```(?:json)?|```$", "", raw.strip()))
        except ValueError:
            print("unparsable", key, raw[:120]); continue
        scores[key] = data
        print(key, {k: data.get(k) for k in ("adherence", "realism", "catchiness", "artifacts")}, flush=True)
        json.dump(scores, open(OUT, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
print("scored", len(scores))
