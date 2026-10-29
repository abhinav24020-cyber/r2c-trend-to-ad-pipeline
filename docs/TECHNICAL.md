# R2C.ai — Social Media Automation

**Foundation, Data Quality & Trend Intelligence** — Gate 1 and Gate 2 of the v2 pipeline.

```
01 Trend pipeline → 02 Versioned AIDA social-post prompts → 03 Image generation
```

The workflow is organized by stage. The trend pipeline creates the ranked-trend
JSON, the prompt generator writes a separate social-post JSON for awareness,
interest, desire, and action, and the image generator uses each post's image brief
to create local campaign-image samples.

This layer **augments** the existing system. No scraper is re-architected:
`trend_pipeline/ingest.py` adapts each existing scraper's native output into one
intermediate schema, and the new gates sit between existing stages.

---

## Quick start

```bash
pip install -r requirements.txt
```

Fill in the private `.env` file in the project root (see `.env.example`): a
Gemini API key for text generation, and a Cloudflare account ID and API token for
image generation. The project loads `.env` automatically; `.env` is git-ignored.
Do not paste keys into chat or commit the `.env` file.

```bash
python -m web_crawler --generate   # crawl -> trends -> 4 posts per trend -> images
python main.py --reset             # trend pipeline on the built-in demo fixture
python main.py --url "https://example.org/research/article" # one page -> post + image
python main.py --url "https://example.org/article1" "https://example.org/article2" --stage interest
python -m web_ui.server            # open the local R2C Studio at http://127.0.0.1:8765
python -m prompt_generator.generate_prompts
python -m image_generator.generate_images
python -m unittest discover -s tests -v
```

**Images** are generated through Cloudflare Workers AI (default model FLUX.2
Klein 4B), one text-free background per post, at the aspect ratio of the post's
AIDA stage:

| Stage | Creative angle | Ratio | Size |
| --- | --- | --- | --- |
| awareness | Evidence-led insight | 16:9 | 1344×768 |
| interest | Method spotlight | 1:1 | 1024×1024 |
| desire | Research-to-practice pathway | 4:5 | 896×1120 |
| action | Problem or research gap | 9:16 | 768×1344 |

Ad copy is typeset over the image separately so it is always legible. Check
source links and the model's terms (bfl.ai/legal) before publishing.

Run on your own scraper output:

```bash
python main.py --reset --input path/to/raw_signals.json
```

The input is a JSON object mapping platform → list of that platform's **native**
records: `{"Reddit": [...], "X": [...], "Instagram": [...], "Facebook": [...], "Google Search": [...]}`.

### Autonomous crawler (Crawlee)

```bash
python -m web_crawler                         # one cycle: crawl -> Gate 1 -> Gate 2
python -m web_crawler --generate              # ...and AIDA copy + image prompts
python -m web_crawler --loop --interval 60    # run unattended, one cycle per hour
python -m web_crawler --crawl-only            # just crawl; inspect data/input/crawled_signals.json
python -m web_crawler --query "AI chips" --query "robotics"   # replace the seed queries
```

`web_crawler/` discovers fresh signals on its own and feeds them to the existing
pipeline in its normal `{platform: [records]}` shape. Each cycle has two phases:

1. **Trend radar** (`web_crawler/trending.py`). Reads what is trending right now:
   Google Trends "trending now" (`CRAWL_TRENDS_GEOS`, default `US,IN`), Bluesky
   trending topics, Mastodon trending hashtags, and Hugging Face trending
   models. AI-relevant trends become search queries, up to
   `CRAWL_MAX_TRENDING_QUERIES`, ahead of the seed queries.
2. **Deep crawl.** Searches every source below for those queries.

| Platform | Source | What is collected |
| --- | --- | --- |
| `Google Trends` | trending-now RSS | trending search term, search volume, attached news stories |
| `Hugging Face` | trending models (+ model card), daily papers | likes/upvotes, downloads, abstract |
| `Bluesky` | public AppView search (top posts) + "What's Hot" feed | likes, reposts, replies, account age |
| `Hacker News` | Algolia search per query + front page; Firebase user lookup | points, comments, author karma and account age |
| `GitHub` | GitHub repository Search API, capped at `CRAWL_GITHUB_MAX_QUERIES` (default 3) | repository description/topics, stars, forks, open issues; recently pushed projects only |
| `News` | Bing News RSS per query | publisher articles (Google News is excluded: its robots.txt disallows crawling) |
| `Mastodon` | trending posts + hashtag timelines (seed tags + one per query) | favourites, boosts, replies, follower count, account age |
| `StackExchange` | hot questions on ai / datascience / AI tags on Stack Overflow | score, answers, reputation, account age |
| `Webpage` | research/industry RSS feeds (arXiv, Hugging Face, OpenAI, TechCrunch, NVIDIA, Microsoft Research, BAIR, DeepMind, Apple ML) and open-web link expansion | full article text |
| `YouTube` | Data API v3 search, only with `YOUTUBE_API_KEY` (100 quota units per query) | views, likes, comments, subscribers |
| `Reddit` | only with `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` (anonymous access is blocked) | score, comments, karma, account age |

Not covered: Twitter/X (Nitter mirrors are gone and the API is paid), and
LinkedIn and Quora (login walls; their terms forbid scraping).

For every News, Hacker News and feed item, Crawlee fetches the real article page
for its prose paragraphs (link lists and site chrome are dropped; up to
`CRAWL_MAX_ARTICLE_CHARS` = 8,000 characters are kept so the content stage sees
methods and findings, not just the introduction). A paywalled or JavaScript-only
page keeps its feed snippet and is flagged `article_fetched: false`. Outbound
links inside articles are followed one hop (`CRAWL_LINK_DEPTH`), ranked so
primary sources (papers, labs, universities, official blogs) are followed before
secondary coverage, and kept only if fresh and on-topic. Crawlee respects
robots.txt, de-duplicates URLs, retries, and caps concurrency, requests per
minute, and total requests per cycle. Every URL passes the same public-address
SSRF check as `--url`.

**Relevance gate.** The keyword list in `settings.py` only decides what is worth
fetching. After the crawl, `web_crawler/relevance.py` has Gemini score every
record 0–100 on how central AI/ML is to it (passing mentions and jokes score
low); records below `CRAWL_MIN_AI_RELEVANCE` (50) are dropped and archived in
`data/input/crawls/crawl_<run>_offtopic.json` for review. The score is stored on
each kept record as `ai_relevance`. Without Gemini, or if a batch call fails,
the records are kept (`--no-relevance-gate` skips the pass entirely).

Each cycle is autonomous: the previous cycle's top trend titles are added as
search queries (disable with `--no-adaptive`). The run does not reset the dedup
history. Resurfaced records are rechecked for quality so they can corroborate
current topics; exact and near-duplicate content is still rejected. Validated
records are upserted by signal ID rather than duplicated. Every crawl is archived
in `data/input/crawls/`, and every cycle's summary is
appended to `data/output/autopilot_runs.jsonl`. `--generate` output goes to
`data/output/autopilot/<run_id>/`. Queries, hashtags, feeds, relevance keywords
and limits live in `web_crawler/settings.py`.

### Start from a webpage or post URL

```bash
python main.py --url "https://example.org/research/article"
```

`--url` accepts one or more public HTML page URLs, extracts each page's title,
description, author, publication date when available, and readable page text,
then runs the existing quality, scoring, and AIDA/image stages. By default it
generates an **action** post and one image; choose another stage with
`--stage awareness|interest|desire|action`.
The scraped source JSON is saved as `data/output/scraped_url_input.json`.

This is a user-selected-source path: attribution, spam, duplicate, credibility,
and freshness checks still run, but a single manually supplied page is not
rejected solely for lacking cross-platform corroboration or a second cluster
member. Its trend record is marked `USER_SUBMITTED_URL` and is not labeled as a
cross-platform-qualified trend. Normal JSON batch runs retain the existing
cross-platform and trend-score qualification rules.

Only public, standard-port HTTP(S) HTML pages are supported. The scraper rejects
private/local network addresses, limits response size and redirects, and does
not bypass logins, paywalls, CAPTCHA, or JavaScript-only rendering. Some social
network post pages may block automated requests or return little readable text;
in that case use a publicly accessible page or the existing JSON input route.

### Use the visual campaign studio

Start the local interface from the project root:

```bash
python -m web_ui.server
```

Open `http://127.0.0.1:8765` in your browser. You can either paste public URLs
or use **Discover automatically** to run the Crawlee trend radar and source
searches without entering URLs. Leave the optional topic field blank to use the
configured AI-research queries, or provide comma-separated topics. Automatic
discovery uses the normal cross-platform trend qualification rules; manual URLs
remain a user-selected-source path.

After discovery, choose a research topic, adjust the AIDA stage, creative
palette, and publishing format, then review or edit the Gemini prompt before
drafting the post. The image brief asks the selected provider for a detailed,
evidence-grounded, text-free editorial background; edit it before generating
the visual. The final downloadable studio creative uses crisp, browser-rendered
typography layered over the generated background, avoiding reliance on image
models to spell ad copy correctly.

The studio binds only to localhost. It calls the existing Python pipeline
directly, keeps API credentials server-side, and stores each UI run in its own
folder under `data/output/ui_runs/` without overwriting the standard
`data/output/` artifacts. Automatic discovery makes real network requests and
can take several minutes; source availability, API credentials, robots rules,
and each provider's usage terms still apply. This local studio is not an
internet-facing, multi-user company deployment: add authentication, managed
secrets, durable job handling, retention controls, and organization-specific
source/compliance review before exposing it to a team. Stop the server with Ctrl+C.

| Flag | Effect |
| --- | --- |
| `--reset` | Clear stored artefacts and dedup history for a clean run |
| `--keep-history` | With `--reset`, preserve the dedup post-history window |
| `--input PATH` | Use a raw scraper JSON file instead of the fixture |
| `--no-json` | Suppress the final JSON dump (keep the readable report) |
| `--verbose` | DEBUG — show every grouping and clustering similarity decision |

---

## What's in the box

| Folder / file | Role |
| --- | --- |
| `web_crawler/` | Step 0: autonomous Crawlee discovery crawler and the `--loop` autopilot |
| `trend_pipeline/` | Step 1: source adapters, manual public-URL extraction, Gate 1/2, scoring, storage, and Gemini client |
| `trend_pipeline/config.py` | Thresholds, weights, credentials, model settings, and paths |
| `main.py` | Simple root command to run the trend pipeline |
| `prompt_generator/` | Step 2: Gemini social writer, JSON validation, and prompt JSON writer |
| `prompt_generator/prompts/v1/` | Versioned system/developer prompts for all four AIDA stages |
| `image_generator/` | Step 3: Cloudflare Workers AI image generation, one image per post at its stage's aspect ratio |
| `app.py`, `templates/`, `static/` | Flask preview UI (port 5000) |
| `web_ui/` | R2C Studio UI (port 8765) |
| `data/input/crawls/` | Every crawl's raw records, plus the records the relevance gate removed |
| `data/output/` | Validated/rejected/ranked JSON, SQLite store, autopilot run log |
| `data/output/autopilot/<run_id>/` | One `--generate` run: `generated_prompts.json`, `generated_ads.json`, `images/` |
| `tests/` | Unit and integration tests; no live API calls required |

### JSON files through the workflow

1. `data/input/crawled_signals.json`: the latest crawl, platform names mapped to
   arrays of platform-native records (archived per run in `data/input/crawls/`).
2. `data/output/ranked_trends.json`: Step 1 output array of qualified trends,
   scores, key facts, entities, and source attribution with excerpts.
3. `generated_prompts.json`: Step 2 output array with one record per trend and
   AIDA stage, each with its `creative_angle`, `aspect_ratio` and a `post` object
   (`short_version`, `long_version`, `hook`, `hashtags`, `target_persona`,
   `source_insight`, `image_prompt`).
4. `generated_ads.json`: Step 3 output with the post, image prompt, model,
   aspect ratio, image size, image path, status, and any error.

---

## Gate 1 — Data quality

Checks run in sequence; the first failure stops the chain and the signal is dropped
with a specific status. Attribution runs first (cheapest, and an unattributed signal
can never be credited or corroborated anyway).

| # | Check | Rejection status |
| --- | --- | --- |
| 1 | Attribution — url, author, platform, timestamp all present | `REJECT_UNATTRIBUTED` |
| 2 | Spam / bot — promo keywords, URL and hashtag stuffing, caps, token repetition, bot-shaped accounts | `REJECT_SPAM` |
| 3 | Exact duplicate — MD5 of normalised text | `REJECT_DUPLICATE` |
| 4 | Near-duplicate — MinHash/LSH **or** TF-IDF cosine vs. post history | `REJECT_DUPLICATE` |
| 5 | Credibility < 40 — account age, engagement/follower ratio, domain authority | `REJECT_LOW_CREDIBILITY` |
| 6 | Freshness — exponential decay `e^(-λt)`, hard cutoff at 72 h | `REJECT_STALE` |
| 7 | Structured enrichment — keywords, hashes, metrics (never rejects) | — |
| 8 | **Cross-platform corroboration ≥ 2 platforms**, decided on Gate 2's semantic topic clusters (`gate1_cross.py`) | `REJECT_SINGLE_SOURCE` |

Older output files may contain the legacy `REJECT_ALREADY_PROCESSED` status.
Current runs recheck resurfaced historical signals so they can provide current
cross-platform evidence; prior processing alone does not make a signal duplicate.

**Credibility** is a weighted blend (age 0.30, engagement ratio 0.35, domain authority
0.35). The engagement sub-score punishes both dead accounts and implausible spikes:
above a 0.60 engagement-to-follower ratio a signal scores 20, since that pattern
indicates amplification rather than reach.

**Cross-platform grouping** is decided on the same semantic topic clusters that
Gate 2 later scores (`gate2_cluster.build_clusters`), not on a separate lexical
grouping. The earlier keyword/TF-IDF union-find linked any two AI stories that
shared vocabulary; on live crawls one group swallowed most of the batch and the
rule confirmed almost everything. Clustering once, with embeddings, means "seen
on two platforms" is a statement about one story. The clustering is
order-independent, and a test asserts that reversing the input changes nothing.

---

## Gate 2 — Trend intelligence

Each signal's **lead** (title plus the first `CLUSTER_EMBED_CHARS` = 700
characters) is embedded with `gemini-embedding-001`; embedding whole articles
pulled vectors toward publisher boilerplate and made unrelated stories from one
site cluster together. Clustering then runs in three passes:

1. **Average-linkage agglomeration.** Two clusters merge while the *mean*
   pairwise cosine between their members clears the threshold, so a chain of
   loosely similar posts cannot drag two stories into one blob.
2. **Same-story merge.** Cluster pairs in the ambiguous band
   (`CLUSTER_MERGE_CANDIDATE_COSINE` = 0.60 up to the threshold) are shown to
   Gemini, which merges only those reporting the same specific release, paper,
   incident or deal. Merges are honoured only along pairs the embeddings already
   call close, so a wrong model answer cannot fuse unrelated topics.
3. **Cross-platform rule** (Gate 1 part B, above).

Surviving clusters get a Gemini-written title, a 2–3 sentence summary, up to five
**key facts** tagged with their source platform, and the named **entities** their
headlines share (`topic_entities`, e.g. `Kimi K3.1 Pro`, `Moonshot AI`), then are
scored with the configured Gemini text model (default `gemini-3.5-flash-lite`;
override with `GEMINI_TEXT_MODEL`) and ranked:

```
Composite = 0.20·credibility + 0.20·growth + 0.15·recency
          + 0.20·cross_platform + 0.15·research_relevance + 0.10·r2c_relevance
```

| Sub-score | How it is computed |
| --- | --- |
| Credibility | Mean Gate 1 credibility of the cluster's signals |
| Growth rate | Platform-weighted mentions (`PLATFORM_SIGNAL_WEIGHT`: a news article or trending model counts more than a short social post) in the recent half-window vs. the older half; flat ≈ 50, tripling = 100, plus a small volume bonus |
| Recency | Mean Gate 1 freshness decay, rescaled to 0–100 |
| Cross-platform | 2 platforms = 80, 3 = 90, 4+ = 100 |
| Research relevance | Gemini scores the topic against technical/research criteria |
| R2C relevance | Gemini scores fit with R2C's audience and business domain |

Clusters scoring **≥ 70.0** whose R2C relevance is at least
`MIN_R2C_RELEVANCE_SCORE` (40) are ranked and emitted as
`QUALIFIED_FOR_WEEK_2_GENERATION`. Everything else is dropped with its score and
a reason: `DROPPED_BELOW_THRESHOLD`, or `DROPPED_OFF_BRAND` for a topic that
trended but is a poor fit for R2C's audience (brand fit is only 10% of the
composite, so a hot but off-brand story could otherwise reach content generation).

Each ranked trend's `source_attribution` entries now carry the source's headline
(`source_insight`) and the first 600 characters of its text (`excerpt`), so the
content stage can quote real evidence rather than a two-sentence summary.

---

## Running without a Gemini key

With no `GEMINI_API_KEY` (or no `google-genai` installed), the pipeline **still runs
end to end** on deterministic local fallbacks: hashed lexical vectors for embeddings,
extractive titles and summaries, and keyword-based relevance scores. Every run prints
its mode and each log line says which path produced a result, so a fallback number is
never mistaken for a model judgement.

Set `ALLOW_OFFLINE_FALLBACK = False` in `trend_pipeline/config.py` to make missing
credentials a hard error instead.

Because the two backends put related text on different parts of the cosine range,
clustering uses two calibrated thresholds — `CLUSTER_SIMILARITY_THRESHOLD` (0.72,
Gemini) and `CLUSTER_SIMILARITY_THRESHOLD_LEXICAL` (0.30, offline). `gate2_cluster`
picks the right one from the active client mode. The Gemini value was checked
against a live crawl (25 Oct 2026, 91 validated signals, lead-only embeddings):
same-story pairs scored 0.81–0.95, related-but-different stories 0.62–0.70. The
same-story merge pass is skipped offline.

All Gemini calls go through `trend_pipeline/gemini_client.py`, which retries with
exponential backoff (4 attempts, 1s → 16s) and degrades rather than crashing.
Embedding requests are split into batches of at most 100 items and 80,000 characters,
with a one-minute pause between batches to stay within the default free-tier quota.

---

## Output contract (Week 2 handoff)

`data/output/ranked_trends.json` — the array consumed by prompt generation:

```json
{
  "trend_id": "trd_20260912_001_99e3",
  "rank": 1,
  "topic_title": "Autonomous agent benchmark results published",
  "topic_summary": "...",
  "composite_trend_score": 85.71,
  "minimum_threshold_applied": 70.0,
  "scoring_breakdown": { "credibility_score": 86.53, "growth_rate_score": 95.0, "...": 0 },
  "weighted_contributions": { "credibility_score": 17.31, "...": 0 },
  "cross_platform_validation": {
    "platform_count": 4,
    "platforms": ["Facebook", "Reddit", "Google Search", "X"],
    "meets_minimum_rule": true
  },
  "growth_evidence": { "recent_mentions": 4, "older_mentions": 0 },
  "supporting_signal_ids": ["sig_fb_3b74ad2a77", "..."],
  "source_attribution": [{ "signal_id": "...", "platform": "...", "source_url": "..." }],
  "status": "QUALIFIED_FOR_WEEK_2_GENERATION"
}
```

Every trend carries full attribution back to the signals that justify it, so Week 2
can ground generated content strictly in validated source data — and every score is
shown with its breakdown and weighted contribution, so a ranking can always be
explained.

---

## Steps 2 and 3 — Advertisement prompt and image generation

The two stages are separate so you can inspect and edit generated posts and image
briefs before generating art. Prompt files are versioned under
`prompt_generator/prompts/v1/`; each AIDA file contains a `SYSTEM_PROMPT` and
`DEVELOPER_PROMPT`, composed with the same named context variables:
`{cluster_label}`, `{cluster_description}`, `{evidence}`, `{target_persona}`,
`{hook_pattern}`, `{cta_pattern}`, and `{brand_voice_notes}`.

By default Step 2 generates all four stages for each qualified trend. For N input
trends this creates 4N post records. Use `--stage awareness`, `--stage interest`,
`--stage desire`, or `--stage action` to generate just one stage. The generator
parses the model reply as JSON and rejects invalid output, extra/missing fields,
overlong short copy, long copy outside 600-1200 characters, more than two
hashtags, prohibited hype terms, brand mentions in awareness, or action copy
without R2C.AI and a measurable one-item CTA.

Every generated post has exactly these model-produced fields: `aida_stage`,
`short_version`, `long_version`, `hook`, `hashtags`, `target_persona`,
`source_insight`, and `image_prompt`. The saved workflow record adds trend ID,
generation status, error, and timestamp around that post object. Image prompts
now request detailed, evidence-grounded visual direction (subject, metaphor,
composition, perspective, lighting, materials, palette, and negative space).
The studio offers research-safe creative angles inspired by established ad
concept frameworks: evidence-led insight, method spotlight, research gap,
evidence comparison, research-to-practice pathway, and open question. It avoids
unsupported urgency, testimonials, stats, and claims. The generated art is
text-free; the studio typesets campaign copy separately.

In batch generation each AIDA stage gets a different creative angle (awareness:
evidence-led insight; interest: method spotlight; desire: research-to-practice
pathway; action: problem or research gap), and the image brief is told the frame
it will be rendered in (16:9, 1:1, 4:5 or 9:16). The writer receives the trend's
key facts, named entities and excerpts from its top five sources, not just the
summary.

Set `GEMINI_API_KEY`, `CLOUDFLARE_ACCOUNT_ID` and `CLOUDFLARE_API_TOKEN` in
`.env`, run the trend pipeline to create ranked trends, then:

```bash
python -m prompt_generator.generate_prompts
python -m image_generator.generate_images
```

The prompt generator uses the configured Gemini text model, default
`gemini-3.5-flash-lite`. Image generation uses Cloudflare Workers AI with
FLUX.2 Klein 4B by default (`CLOUDFLARE_IMAGE_MODEL`); FLUX.1 schnell also works
but is square-only. Cloudflare has a free daily allocation, then bills per use.
Readable text generation by image models is unreliable, so campaign copy is added
separately; review each image before publishing.

Both commands accept `--input` and `--output`; the image command also accepts
`--images-dir`. Failed entries remain in the output with an explicit error; they
are never silently skipped.

### Storage

| Artefact | Backend | Semantics |
| --- | --- | --- |
| `data/output/validated_signals.json` | JSON | This cycle's snapshot (overwritten each run) |
| `data/output/rejected_signals.json` | JSON | This cycle's rejections + reasons (Week 3 learning input) |
| `data/output/ranked_trends.json` | JSON | This cycle's ranked trends |
| `data/output/signals.db` | SQLite | **Accumulates** across cycles (upsert), indexed and queryable |
| `data/output/post_history.json` | JSON | Rolling 500-item window feeding near-duplicate detection |

---

## Demo script

```bash
python main.py --reset      # 20 raw records in → 3 ranked trends out
```

What it shows, in one run:

1. **20 raw signals** normalised from five platforms in their native formats.
2. **10 rejected at Gate 1**, one per failure mode, each with a specific reason:
   2 spam, 2 duplicates, 2 low credibility, 1 stale, 1 unattributed, 2 single-source.
   Validation pass rate: **50%**.
3. **10 validated signals** stored with full source attribution.
4. **3 topic clusters** formed — the stipend thread is correctly excluded at Gate 1
   for appearing on Reddit only, despite having strong engagement.
5. **3 ranked trends**, top-scoring being the agent benchmark story corroborated
   across all four platforms at **85.71**.

Then run `python main.py` again with no flags: resurfaced signals are revalidated and
upserted by ID, so stored records are not duplicated while current corroboration can
be refreshed.

---

## Known limitations

- **Fixed weights.** The six trend-score weights are static in
  `trend_pipeline/config.py`. Week 3's learning loop is what re-weights them from
  performance data.
- **Clustering is O(n²) in memory and time.** Average-linkage agglomeration holds a
  full pairwise similarity matrix; fine for the hundreds of signals a cycle produces,
  not for tens of thousands. At that scale it wants an ANN index and a sparse merge.
  Review cluster evidence before using trends downstream.
- **Thresholds are calibrated, not learned.** The clustering cutoffs were set from the
  observed similarity separation on one live crawl. They should be re-checked as the
  source mix changes.
- **No memory between cycles.** Growth is measured inside one cycle's window, so a
  story's velocity across cycles is not tracked and a story can re-qualify in a later
  cycle if new signals about it appear.
- **Growth rate needs history.** With a single scrape, all mentions land in the recent
  half-window and velocity saturates. Real velocity measurement wants successive cycles.
- **Domain authority is a static table.** `config.DOMAIN_AUTHORITY` covers the common
  research and tech domains; everything else falls back to a per-platform baseline.
