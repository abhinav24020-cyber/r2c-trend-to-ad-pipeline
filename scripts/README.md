# Image model comparison scripts

Run from the repository root, in this order:

```bash
python scripts/compare_render_model.py klein9b   # or klein4b, sdxl, sd35, qwen: 9 ads into data/output/samples/<model>/
python scripts/compare_score_models.py           # Gemini vision scores every picture -> data/output/samples/model_scores.json
python scripts/compare_build_report.py           # -> data/output/samples/R2C_image_model_comparison.pdf
```

Klein 4B/9B and SDXL run on Cloudflare Workers AI (CLOUDFLARE_* in .env). SD 3.5 Medium and Qwen-Image-2512 run
on their official Hugging Face Spaces (HF_TOKEN in .env). Each model gets the same prompts and the same R2C poster
template, one image per trend; finished ads are kept, so a run interrupted by a quota resumes where it stopped.
