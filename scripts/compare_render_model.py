"""Render the 9 v2 ads (same prompts, poster template) with another image model, one image per trend.

usage: python make_models.py <sdxl|sd35|qwen|klein9b>
"""
import json, logging, os, sys
sys.path.insert(0, os.getcwd())
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from trend_pipeline import config
from image_generator import generate_images

MODELS = {
    "sdxl": {"folder": "stable-diffusion-xl-base-1.0", "name": "Stable Diffusion XL base 1.0",
             "where": "Cloudflare Workers AI (@cf/stabilityai/stable-diffusion-xl-base-1.0)",
             "client": lambda: generate_images.CloudflareImageClient(
                 config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN,
                 "@cf/stabilityai/stable-diffusion-xl-base-1.0", 20)},
    "sd35": {"folder": "stable-diffusion-3.5-medium", "name": "Stable Diffusion 3.5 Medium",
             "where": "its official Hugging Face Space (free ZeroGPU), 40 steps",
             "client": lambda: generate_images.HuggingFaceSpaceClient(
                 "stabilityai/stable-diffusion-3.5-medium", "stabilityai/stable-diffusion-3.5-medium",
                 config.HF_TOKEN, steps=40)},
    "qwen": {"folder": "qwen-image-2512", "name": "Qwen-Image-2512",
             "where": "its official Hugging Face Space (free ZeroGPU), 50 steps, prompt enhancer off",
             "client": lambda: generate_images.HuggingFaceSpaceClient(
                 "Qwen/Qwen-Image-2512", "Qwen/Qwen-Image-2512", config.HF_TOKEN,
                 aspects=["1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3"], extra={"prompt_enhance": False})},
    "klein4b": {"folder": "flux-2-klein-4b-v2", "name": "FLUX.2 Klein 4B",
                "where": "Cloudflare Workers AI (@cf/black-forest-labs/flux-2-klein-4b)",
                "client": lambda: generate_images.CloudflareImageClient(
                    config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN,
                    "@cf/black-forest-labs/flux-2-klein-4b", config.CLOUDFLARE_IMAGE_STEPS)},
    "klein9b": {"folder": "flux-2-klein-9b-v2", "name": "FLUX.2 Klein 9B",
                "where": "Cloudflare Workers AI (@cf/black-forest-labs/flux-2-klein-9b)",
                "client": lambda: generate_images.CloudflareImageClient(
                    config.CLOUDFLARE_ACCOUNT_ID, config.CLOUDFLARE_API_TOKEN,
                    "@cf/black-forest-labs/flux-2-klein-9b", config.CLOUDFLARE_IMAGE_STEPS)},
}
key = sys.argv[1]
spec = MODELS[key]
if key in ("sd35", "qwen") and not config.HF_TOKEN:
    raise SystemExit("HF_TOKEN is not set in .env")

SOURCE = os.path.join("data", "output", "samples", "flux-2-klein-9b-v2", "samples.json")
out = os.path.join("data", "output", "samples", spec["folder"])
os.makedirs(out, exist_ok=True)
v2 = json.load(open(SOURCE, encoding="utf-8"))
client = spec["client"]()

results_path = os.path.join(out, "samples.json")
previous = {}
if key != "klein9b" and os.path.isfile(results_path):
    previous = {r["record"]["trend_id"]: r for r in json.load(open(results_path, encoding="utf-8"))}
results = []
for i, item in enumerate(v2, 1):
    record = item["record"]
    done = previous.get(record["trend_id"])
    if done and (done.get("result") or {}).get("status") == "success":  # resume: keep finished ads
        results.append(done)
        continue
    result = generate_images.generate_image_record(record, client, out)
    result["image_model_name"] = spec["name"]
    print("[{}/{}] {} -> {} {}".format(i, len(v2), item["trend"], result["status"],
                                       result.get("ad_path") or (result.get("error_message") or "")[:160]), flush=True)
    results.append({"trend": item["trend"], "record": record, "result": result})
    json.dump(results, open(results_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
json.dump(results, open(results_path, "w", encoding="utf-8"), indent=2, ensure_ascii=False)

lines = ["# R2C.Ai ads: prompt → image ({})".format(spec["name"]), "",
         "Model: **{}** via {}. Same v2 prompts and poster template as every other model in "
         "`samples/`, one image per trend.".format(spec["name"], spec["where"]), ""]
for n, it in enumerate(results, 1):
    rec, res = it["record"], it["result"] or {}
    p = rec.get("post") or {}
    lines += ["---", "", "## {}. {}".format(n, it["trend"]), "",
              "**Format:** {} · {} · **Theme:** {} · **Model:** {}".format(
                  rec["aspect_ratio"], rec["placement"], rec.get("theme"), spec["name"]), "",
              "**Ad text:** {} · *{}* · Sticker: {} · CTA: {}".format(
                  p.get("eyebrow"), p.get("headline"), p.get("sticker"), p.get("cta")), "",
              "### Prompt", "", "```text", str(rec.get("image_prompt")), "```", "", "### Image", ""]
    lines += ["![{}]({})".format(p.get("headline"), os.path.basename(res["ad_path"])) if res.get("ad_path")
              else "(failed: {})".format(res.get("error_message")), ""]
open(os.path.join(out, "prompts_and_images.md"), "w", encoding="utf-8").write("\n".join(str(x) for x in lines))
ok = sum(1 for r in results if (r.get("result") or {}).get("status") == "success")
print("DONE {}: {}/{} ads in {}".format(spec["name"], ok, len(results), os.path.abspath(out)))
