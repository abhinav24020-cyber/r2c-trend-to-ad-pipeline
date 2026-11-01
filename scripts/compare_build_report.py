"""Detailed image-model comparison PDF: same 9 prompts on every model, scores, per-model and per-trend views."""
import json, os, sys
from xml.sax.saxutils import escape
from PIL import Image as PILImage
from reportlab.graphics.charts.barcharts import VerticalBarChart
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

sys.path.insert(0, os.getcwd())
from trend_pipeline import config

SAMPLES = os.path.join("data", "output", "samples")
OUT = os.path.join(SAMPLES, "R2C_image_model_comparison.pdf")
# folder, short name, developer, size, where it ran, settings, cost
MODELS = [
    ("flux-2-klein-9b-v2", "FLUX.2 Klein 9B", "Black Forest Labs", "9B parameters",
     "Cloudflare Workers AI", "Default (distilled, few steps)",
     "Free daily allowance (about 7 images/day); about $0.015/image on Workers Paid"),
    ("20261029_1352", "FLUX.2 Klein 4B", "Black Forest Labs", "4B parameters",
     "Cloudflare Workers AI", "Default (distilled, few steps); earlier v1 prompts and dark template",
     "Free daily allowance (about 95 images/day); about $0.001/image on Workers Paid"),
    ("stable-diffusion-3.5-medium", "SD 3.5 Medium", "Stability AI", "2.5B parameters (MMDiT)",
     "Official Hugging Face Space (free ZeroGPU)", "40 steps, guidance 4.5",
     "Free GPU quota (a few images per day); about $0.035/image via Replicate"),
    ("stable-diffusion-xl-base-1.0", "SDXL base 1.0", "Stability AI", "about 3.5B parameters (UNet + 2 text encoders)",
     "Cloudflare Workers AI", "20 steps, guidance 7.5", "Free on Cloudflare"),
]
# Considered but not run (shown in the model table and explained, not scored).
NOT_TESTED = [
    ("Qwen-Image-2512", "Alibaba Qwen", "20B parameters (MMDiT)",
     "Official Hugging Face Space (free ZeroGPU) / HF Inference Providers (fal-ai, WaveSpeed)", "-",
     "Not tested: needs paid GPU time (about $0.02-0.03/image via fal-ai or WaveSpeed)"),
]
OLDER_PROMPT_FOLDERS = {"20261029_1352"}  # rendered from the earlier v1 prompts, not the shared v2 prompts
NAVY, BLUE, GREY = colors.HexColor("#0A1F3C"), colors.HexColor("#3B5DE7"), colors.HexColor("#5B6B86")
SERIES = [colors.HexColor(c) for c in ("#3B5DE7", "#FF6B35", "#2EA44F", "#B04BD8")]
LOGO = os.path.join("image_generator", "assets", "r2c_logo_navy.png")
TMP = os.path.join(os.environ.get("TEMP", "."), "r2c_compare_jpgs")
os.makedirs(TMP, exist_ok=True)
CRITERIA = ["adherence", "realism", "catchiness", "artifacts"]

runs = {}
for folder, *_ in MODELS:
    path = os.path.join(SAMPLES, folder, "samples.json")
    runs[folder] = json.load(open(path, encoding="utf-8")) if os.path.isfile(path) else []
scores_path = os.path.join(SAMPLES, "model_scores.json")
scores = json.load(open(scores_path, encoding="utf-8")) if os.path.isfile(scores_path) else {}
notes_path = os.path.join(SAMPLES, "model_recommendation.json")
notes = json.load(open(notes_path, encoding="utf-8")) if os.path.isfile(notes_path) else {}
base = runs["flux-2-klein-9b-v2"]

ss = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=ss["Title"], textColor=NAVY, fontSize=20, leading=25, alignment=0)
H2 = ParagraphStyle("H2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, leading=16, spaceBefore=5, spaceAfter=4)
H3 = ParagraphStyle("H3", parent=ss["Heading4"], textColor=BLUE, fontSize=8.8, leading=11, spaceBefore=4, spaceAfter=2)
BODY = ParagraphStyle("Body", parent=ss["BodyText"], fontSize=8.7, leading=11.4, spaceBefore=0, spaceAfter=3)
SMALL = ParagraphStyle("Small", parent=BODY, fontSize=7.6, leading=9.6, textColor=GREY)
CELL = ParagraphStyle("Cell", parent=BODY, fontSize=7.5, leading=9.4, spaceAfter=0)
CELLB = ParagraphStyle("CellB", parent=CELL, fontName="Helvetica-Bold")
BULLET = ParagraphStyle("Bullet", parent=BODY, leftIndent=11, bulletIndent=1, spaceAfter=1)
PROMPT = ParagraphStyle("Prompt", parent=BODY, fontName="Courier", fontSize=6.1, leading=7.6, spaceAfter=4,
                        backColor=colors.HexColor("#F2F4F9"), borderPadding=4,
                        borderColor=colors.HexColor("#D5DBE8"), borderWidth=0.5)


def p(text, style=BODY):
    return Paragraph(text, style)


def fitted(path, max_w, max_h):
    run_dir = os.path.basename(os.path.dirname(os.path.abspath(path)))
    out = os.path.join(TMP, run_dir + "__" + os.path.basename(path).replace(".png", ".jpg"))
    with PILImage.open(path) as im:
        w, h = im.size
        if not os.path.isfile(out) or os.path.getmtime(out) < os.path.getmtime(path):
            im.convert("RGB").resize((min(w, 900), int(h * min(w, 900) / w))).save(out, "JPEG", quality=84)
    s = min(max_w / w, max_h / h)
    img = Image(out, width=w * s, height=h * s)
    img.hAlign = "CENTER"
    return img


def grid(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D5DBE8")),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3)]
    if header:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8ECF7")))
    t.setStyle(TableStyle(st))
    return t


def made(folder):
    return [it for it in runs[folder] if (it.get("result") or {}).get("status") == "success"]


def score_of(folder, trend_id):
    return scores.get("{}|{}".format(folder, trend_id))


def averages(folder):
    vals = [v for v in (score_of(folder, it["record"]["trend_id"]) for it in made(folder)) if v]
    if not vals:
        return None
    avg = {c: sum(float(v.get(c, 0)) for v in vals) / len(vals) for c in CRITERIA}
    avg["overall"] = sum(avg[c] for c in CRITERIA) / len(CRITERIA)
    avg["n"] = len(vals)
    return avg


def chart(rows):
    """Grouped bars: one group per criterion, one bar per model."""
    d = Drawing(17 * cm, 6.2 * cm)
    bc = VerticalBarChart()
    bc.x, bc.y, bc.width, bc.height = 30, 40, 11.5 * cm, 4.6 * cm
    bc.data = [[avg[c] for c in CRITERIA + ["overall"]] for avg, _ in rows]
    bc.categoryAxis.categoryNames = [c.capitalize() for c in CRITERIA] + ["Overall"]
    bc.categoryAxis.labels.fontSize = 7
    bc.valueAxis.valueMin, bc.valueAxis.valueMax, bc.valueAxis.valueStep = 0, 10, 2
    bc.valueAxis.labels.fontSize = 7
    bc.groupSpacing, bc.barSpacing = 8, 1
    for i in range(len(rows)):
        bc.bars[i].fillColor = SERIES[i % len(SERIES)]
        bc.bars[i].strokeColor = None
    d.add(bc)
    lg = Legend()
    lg.x, lg.y = 12.6 * cm, 5.2 * cm
    lg.fontSize, lg.boxAnchor, lg.columnMaximum = 7.5, "nw", 6
    lg.colorNamePairs = [(SERIES[i % len(SERIES)], name) for i, (_, name) in enumerate(rows)]
    d.add(lg)
    return d


FULL_W = A4[0] - 3.4 * cm
story = []
ranked = sorted(((averages(f), n, f) for f, n, *_ in MODELS), key=lambda x: -(x[0] or {}).get("overall", -1))
scored = [(a, n) for a, n, _ in ranked if a]

# ------------------------------------------------------------- 1. summary
logo = fitted(LOGO, 4.2 * cm, 1.6 * cm)
logo.hAlign = "LEFT"
story += [logo, Spacer(1, 6), p("Image model comparison for R2C.Ai ads: detailed report", H1),
          p("Same 9 prompts, same brand template, one image per trend per model. Ad copy and layout are identical; "
            "only the image model changes.", SMALL), Spacer(1, 6)]
story.append(p("Executive summary", H2))
if notes.get("summary"):
    story.append(p(notes["summary"], BODY))
rank_rows = [[p(x, CELLB) for x in ("Rank", "Model", "Ads made", "Overall score (1-10)")]]
for i, (avg, name, folder) in enumerate(ranked, 1):
    rank_rows.append([p(str(i) if avg else "-", CELL), p("<b>{}</b>".format(name), CELL),
                      p("{} of 9".format(len(made(folder))), CELL),
                      p("{:.1f}".format(avg["overall"]) if avg else "not scored (no images)", CELL)])
story.append(grid(rank_rows, [1.3 * cm, 5 * cm, 2.6 * cm, FULL_W - 8.9 * cm]))

story.append(p("1. Models tested", H2))
rows = [[p(x, CELLB) for x in ("Model", "Developer", "Size", "Where it ran", "Settings", "Cost")]]
for folder, name, dev, size, where, settings, cost in MODELS:
    rows.append([p("<b>{}</b>".format(name), CELL), p(dev, CELL), p(size, CELL), p(where, CELL), p(settings, CELL),
                 p(cost, CELL)])
for name, dev, size, where, settings, cost in NOT_TESTED:
    rows.append([p("<b>{}</b> (not tested)".format(name), CELL), p(dev, CELL), p(size, CELL), p(where, CELL),
                 p(settings, CELL), p(cost, CELL)])
story.append(grid(rows, [2.5 * cm, 2.0 * cm, 2.4 * cm, 2.8 * cm, 2.6 * cm, FULL_W - 12.3 * cm]))
story.append(p("Why Qwen-Image-2512 was not tested", H3))
story.append(p("Qwen-Image-2512 is a 20-billion-parameter model, more than twice the size of the largest model "
               "tested here. Its official Hugging Face Space reserves 240 seconds of GPU per image, which is more "
               "than a free Hugging Face account is allowed, and the paid Inference Providers route needs a "
               "Hugging Face token with inference permission and pre-paid credits (about $0.02-0.03 per image), "
               "which were not available for this test. It can be added to this report later on the same 9 prompts.",
               BODY))

story.append(p("2. Method", H2))
for b in [
    "Prompts: the 9 v2 prompts (real-photo look, rich scene, colour theme per ad), written by Gemini "
    "({}) and assembled by code. Every model received exactly the same prompt text.".format(config.GEMINI_TEXT_MODEL),
    "Each model's text-free picture was placed into the same R2C poster template (logo, headline, services, CTA), so "
    "the differences come only from the image model.",
    "Scoring: Gemini vision looked at each raw picture together with its prompt and scored 1-10 on prompt adherence, "
    "realism (not obviously AI-generated), catchiness and artifacts (10 = no visible defects). Overall = the mean of "
    "the four. This is an automated second opinion; the side-by-side pages let a person check it.",
    "One image per trend per model (no cherry-picking, no retries), to keep API cost low.",
    "FLUX.2 Klein 4B: Cloudflare's free daily allowance ran out before it could be re-run on the v2 prompts, so its "
    "9 ads come from the earlier run (v1 prompts: the same trends, ad copy and formats, but the earlier picture ideas, "
    "a navy/blue colour grade and the dark template). Its pictures were scored against their own v1 prompts, so its "
    "adherence score is comparable, but its look is not a like-for-like comparison.",
    "Limitations: 9 images per model is a small sample; SDXL's text encoder reads only about the first 75 tokens, so "
    "it ignores the later parts of these long prompts.",
]:
    story.append(Paragraph(b, BULLET, bulletText="•"))
story.append(PageBreak())

# ------------------------------------------------------------- 3. scorecard
story.append(p("3. Scorecard", H2))
rows = [[p(x, CELLB) for x in ("Model", "Images scored", "Adherence", "Realism", "Catchiness", "Artifacts", "Overall")]]
for avg, name, folder in ranked:
    if avg:
        rows.append([p("<b>{}</b>".format(name), CELL), p(str(avg["n"]), CELL)]
                    + [p("{:.1f}".format(avg[c]), CELL) for c in CRITERIA] + [p("<b>{:.1f}</b>".format(avg["overall"]), CELL)])
    else:
        rows.append([p("<b>{}</b>".format(name), CELL), p("0", CELL)] + [p("-", CELL)] * 5)
story.append(grid(rows, [3.6 * cm, 2.2 * cm] + [(FULL_W - 5.8 * cm) / 5] * 5))
if scored:
    story += [Spacer(1, 6), chart(scored)]
story.append(p("Per-trend overall scores", H3))
rows = [[p("Trend", CELLB)] + [p(n, CELLB) for _, n, *_ in MODELS]]
for i, it in enumerate(base, 1):
    tid = it["record"]["trend_id"]
    row = [p("{}. {}".format(i, escape(it["trend"][:48])), CELL)]
    for folder, *_ in MODELS:
        sc = score_of(folder, tid)
        row.append(p("{:.1f}".format(sum(float(sc[c]) for c in CRITERIA) / 4) if sc else "-", CELL))
    rows.append(row)
story.append(grid(rows, [6.0 * cm] + [(FULL_W - 6.0 * cm) / len(MODELS)] * len(MODELS)))
story.append(PageBreak())

# ------------------------------------------------------------- 4. per model
story.append(p("4. Results by model", H2))
model_notes = notes.get("models", {})
for folder, name, dev, size, where, settings, cost in MODELS:
    avg = averages(folder)
    head = [p("{} ({}, {})".format(name, dev, size), H2),
            p("Ran on {} · {} · {} of 9 ads made{}".format(
                where, settings, len(made(folder)),
                " · overall {:.1f}/10".format(avg["overall"]) if avg else ""), SMALL)]
    mn = model_notes.get(folder, {})
    bullets = [Paragraph("<b>Strength:</b> " + escape(s), BULLET, bulletText="+") for s in mn.get("strengths", [])]
    bullets += [Paragraph("<b>Weakness:</b> " + escape(s), BULLET, bulletText="−") for s in mn.get("weaknesses", [])]
    story += head + bullets
    items = made(folder)
    if not items:
        failed = runs[folder][0]["result"].get("error_message") if runs[folder] and runs[folder][0].get("result") else ""
        story += [p("No images were generated. Reason: {}".format(escape(str(failed or "not run"))[:300]), BODY),
                  PageBreak()]
        continue
    thumbs, row = [], []
    tw = FULL_W / 3 - 0.2 * cm
    for it in items:
        sc = score_of(folder, it["record"]["trend_id"])
        caption = "{} · {}".format(it["record"]["aspect_ratio"], escape(it["trend"][:38]))
        if sc:
            caption += "<br/>adh {adherence} · real {realism} · catchy {catchiness} · artifacts {artifacts}".format(**sc)
        row.append([fitted(it["result"]["ad_path"], tw, 5.2 * cm), p(caption, SMALL)])
        if len(row) == 3:
            thumbs.append(row)
            row = []
    if row:
        thumbs.append(row + [""] * (3 - len(row)))
    t = Table(thumbs, colWidths=[tw + 0.2 * cm] * 3)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [Spacer(1, 4), t]
    verdicts = [(it, score_of(folder, it["record"]["trend_id"])) for it in items]
    vrows = [[p("Trend", CELLB), p("Gemini's note on the picture", CELLB)]]
    vrows += [[p(escape(it["trend"][:44]), CELL), p(escape(str(sc.get("note", ""))), CELL)] for it, sc in verdicts if sc]
    if len(vrows) > 1:
        story += [p("Reviewer notes", H3), grid(vrows, [5.2 * cm, FULL_W - 5.2 * cm])]
    story.append(PageBreak())

# ------------------------------------------------------------- 5. per trend
story.append(p("5. Side by side, trend by trend", H2))
cell_w = FULL_W / 2 - 0.15 * cm
for i, item in enumerate(base):
    rec = item["record"]
    post = rec["post"]
    story.append(p("Trend {} of {}: {}".format(i + 1, len(base), escape(item["trend"])), H2))
    story.append(p("{} · {} · theme: {} · headline: <i>{}</i>".format(
        rec["aspect_ratio"], rec["placement"], rec.get("theme"), escape(str(post.get("headline")))), SMALL))
    wide = rec["aspect_ratio"] in ("1.91:1", "16:9")
    img_h = 4.3 * cm if wide else 7.4 * cm
    cells = []
    for folder, name, *_ in MODELS:
        match = next((x for x in runs[folder] if x["record"]["trend_id"] == rec["trend_id"]), None)
        res = (match or {}).get("result") or {}
        sc = score_of(folder, rec["trend_id"])
        label = "<b>{}</b>".format(name) + (" (earlier v1 prompt)" if folder in OLDER_PROMPT_FOLDERS else "")
        if sc:
            label += "<br/>adh {adherence} · real {realism} · catchy {catchiness} · art {artifacts}".format(**sc)
        block = [p(label, CELL)]
        block.append(fitted(res["ad_path"], cell_w, img_h) if res.get("ad_path")
                     else p("(not generated: free GPU quota)" if "quota" in str(res.get("error_message", "")).lower()
                            else "(not generated)", SMALL))
        cells.append(block)
    cells += [""] * (4 - len(cells))
    t = Table([cells[0:2], cells[2:4]], colWidths=[cell_w + 0.15 * cm] * 2)
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                           ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]))
    story += [Spacer(1, 3), t, p("Prompt sent to FLUX.2 Klein 9B, SD 3.5 Medium and SDXL (v2)", H3),
              p(escape(rec["image_prompt"]), PROMPT)]
    for folder in OLDER_PROMPT_FOLDERS:
        old = next((x for x in runs[folder] if x["record"]["trend_id"] == rec["trend_id"]), None)
        if old:
            story += [p("Earlier v1 prompt used for FLUX.2 Klein 4B", H3), p(escape(old["record"]["image_prompt"]), PROMPT)]
    story.append(PageBreak())

# ------------------------------------------------------------- 6. recommendation
story.append(p("6. Recommendation", H2))
if notes.get("summary"):
    story.append(p(notes["summary"], BODY))
for b in notes.get("points", []):
    story.append(Paragraph(escape(b), BULLET, bulletText="•"))


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(1.7 * cm, 1.0 * cm, "R2C.Ai · Image model comparison: detailed report")
    canvas.drawRightString(A4[0] - 1.7 * cm, 1.0 * cm, "Page {}".format(doc.page))
    canvas.restoreState()


doc = SimpleDocTemplate(OUT, pagesize=A4, leftMargin=1.7 * cm, rightMargin=1.7 * cm, topMargin=1.4 * cm,
                        bottomMargin=1.6 * cm, title="R2C.Ai image model comparison: detailed report",
                        author="R2C.Ai marketing pipeline")
doc.build(story, onFirstPage=footer, onLaterPages=footer)
print(os.path.abspath(OUT), os.path.getsize(OUT) // 1024, "KB")
