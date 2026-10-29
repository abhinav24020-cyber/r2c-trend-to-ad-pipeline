"""Offline tests for the autonomous crawler: parsing helpers and the new adapters."""
from __future__ import annotations

import logging
import os
import unittest
from datetime import datetime, timedelta, timezone

os.environ["GEMINI_API_KEY"] = ""

from bs4 import BeautifulSoup

from trend_pipeline import config, gate1_quality, ingest
from trend_pipeline import gemini_client as _gemini_client
from web_crawler import crawler, trending

logging.disable(logging.CRITICAL)

# See test_pipeline.py: config may already hold the developer's .env key.
config.GEMINI_API_KEY = ""
_gemini_client._SHARED = None


def _iso_hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


class TestCrawlerHelpers(unittest.TestCase):
    def test_bing_redirect_is_decoded(self) -> None:
        link = (
            "http://www.bing.com/news/apiclick.aspx?ref=FexRss&aid=&tid=abc"
            "&url=https%3a%2f%2fwww.example.com%2fstory%2f42&c=1&mkt=en-us"
        )
        self.assertEqual(crawler.decode_bing_link(link), "https://www.example.com/story/42")
        self.assertEqual(crawler.decode_bing_link("https://example.com/a"), "https://example.com/a")

    def test_rss_dates_are_normalised_to_iso(self) -> None:
        iso = crawler.to_iso("Sun, 18 Oct 2026 01:52:00 GMT")
        self.assertEqual(iso, "2026-10-18T01:52:00+00:00")
        # The pipeline's own parser must accept what the crawler writes.
        self.assertIsNotNone(ingest.parse_timestamp(iso))
        self.assertIsNone(crawler.to_iso("not a date"))

    def test_freshness_window(self) -> None:
        self.assertTrue(crawler.is_fresh(_iso_hours_ago(2)))
        self.assertFalse(crawler.is_fresh(_iso_hours_ago(500)))
        self.assertFalse(crawler.is_fresh(None))

    def test_relevance_is_whole_word(self) -> None:
        self.assertTrue(crawler.is_relevant("New open-weight LLM tops the benchmark"))
        self.assertTrue(crawler.is_relevant("Regulators look at AI safety"))
        self.assertFalse(crawler.is_relevant("Said the chair in Spain"))  # 'ai' inside words

    def test_link_expansion_filter(self) -> None:
        parent = "https://techcrunch.com/2026/10/16/story/"
        self.assertTrue(crawler.expandable_link("https://research.example.org/paper-x", parent))
        self.assertFalse(crawler.expandable_link("https://techcrunch.com/other/", parent))
        self.assertFalse(crawler.expandable_link("https://twitter.com/someone/status/1", parent))
        self.assertFalse(crawler.expandable_link("https://example.org/report.pdf", parent))
        self.assertFalse(crawler.expandable_link("mailto:x@example.org", parent))

    def test_paragraph_extraction_drops_link_lists(self) -> None:
        story = "Researchers released an open benchmark for agent tool use today. " * 4
        html = (
            "<article><p>{}</p><p>{}</p>"
            "<p><a href='/a'>Most popular story number one with a long headline</a> "
            "<a href='/b'>Another popular story that repeats on every page</a></p>"
            "</article>"
        ).format(story, story)
        text = crawler.article_paragraphs(BeautifulSoup(html, "html.parser"))
        self.assertIn("open benchmark", text)
        self.assertNotIn("Most popular", text)

    def test_hacker_news_authors_are_merged(self) -> None:
        result = crawler.CrawlResult()
        result.put("Hacker News", "1", {"objectID": "1", "title": "t", "author": "pg"})
        result.hn_users["pg"] = {"account_age_days": 7000, "karma": 150000}
        record = result.payload()["Hacker News"][0]
        self.assertEqual(record["author"], {"name": "pg", "account_age_days": 7000, "karma": 150000})
        self.assertFalse(result.put("Hacker News", "1", {}))  # de-duplicated by key


class TestTrendingHelpers(unittest.TestCase):
    def test_discovery_coverage_includes_targeted_queries_and_research_feeds(self) -> None:
        self.assertIn("AI agent security", crawler.settings.SEED_QUERIES)
        self.assertIn("LLM evaluation benchmark", crawler.settings.SEED_QUERIES)
        self.assertGreaterEqual(crawler.settings.MAX_TRENDING_QUERIES, 12)
        self.assertGreaterEqual(crawler.settings.GITHUB_MAX_QUERIES, 1)
        for source in (
            "NVIDIA Deep Learning",
            "Microsoft Research",
            "Berkeley AI Research",
            "Google DeepMind",
            "Apple Machine Learning Research",
        ):
            with self.subTest(source=source):
                self.assertIn(source, crawler.settings.RSS_FEEDS)

    def test_github_search_requests_are_capped_and_recent(self) -> None:
        queries = ["AI agents", "open source LLM", "AI benchmark", "MLOps"]
        requests = crawler.build_seed_requests(queries)
        github_requests = [request for request in requests if request.label == crawler.GITHUB_SEARCH]

        self.assertEqual(len(github_requests), crawler.settings.GITHUB_MAX_QUERIES)
        self.assertIn("api.github.com/search/repositories", github_requests[0].url)
        self.assertIn("pushed%3A", github_requests[0].url)
        self.assertEqual(github_requests[0].user_data["query"], queries[0])

    def test_model_id_becomes_search_query(self) -> None:
        self.assertEqual(trending.model_query("deepseek-ai/DeepSeek-V4.1-Flash-1017"),
                         "DeepSeek V4.1 Flash")
        self.assertEqual(trending.model_query("google/embeddinggemma_2"), "embeddinggemma 2")

    def test_google_trends_traffic(self) -> None:
        self.assertEqual(trending.traffic_to_int("200+"), 200)
        self.assertEqual(trending.traffic_to_int("10K+"), 10_000)
        self.assertEqual(trending.traffic_to_int("2M+"), 2_000_000)
        self.assertEqual(trending.traffic_to_int(""), 0)

    def test_bluesky_web_url(self) -> None:
        post = {"uri": "at://did:plc:abc/app.bsky.feed.post/3my5", "author": {"handle": "a.bsky.social"}}
        self.assertEqual(trending.bluesky_url(post), "https://bsky.app/profile/a.bsky.social/post/3my5")

    def test_readme_front_matter_and_markup_removed(self) -> None:
        text = trending.readme_text("---\nlicense: mit\n---\n# Model\n![img](x.png) A **fast** model.")
        self.assertEqual(text, "Model A fast model.")

    def test_trend_priority_prefers_public_trends(self) -> None:
        found = [("Qwen 4", "Hugging Face trending"), ("AI stocks", "Google Trends US"),
                 ("AI art row", "Bluesky trending")]
        self.assertEqual(crawler.pick_trending_queries(found, 2), ["AI stocks", "AI art row"])


class TestCrawlerAdapters(unittest.TestCase):
    def test_trend_source_records_normalise(self) -> None:
        now = _iso_hours_ago(1)
        payload = {
            "Google Trends": [{"id": "US|ai stocks", "query": "ai stocks", "geo": "US",
                               "approx_traffic": 5000, "published_at": now,
                               "news": [{"title": "AI chip stocks rally", "url": "https://www.cnbc.com/x",
                                         "source": "CNBC"}]}],
            "Bluesky": [{"uri": "at://d/app.bsky.feed.post/1", "url": "https://bsky.app/profile/a/post/1",
                         "text": "New open LLM benchmark results are out", "created_at": now,
                         "author": {"handle": "a", "created_at": _iso_hours_ago(24 * 400)},
                         "like_count": 50, "repost_count": 10, "reply_count": 5}],
            "Hugging Face": [{"id": "2610.1", "kind": "paper", "title": "A paper",
                              "summary": "We study agents.", "url": "https://huggingface.co/papers/2610.1",
                              "author": "kostino", "created_at": now, "likes": 30}],
            "StackExchange": [{"question_id": 5, "site": "ai", "link": "https://ai.stackexchange.com/q/5",
                               "title": "How do LLM agents plan?", "body": "Question text here.",
                               "creation_date": int(datetime.now(timezone.utc).timestamp()),
                               "owner": {"display_name": "u", "reputation": 900, "account_age_days": 1000},
                               "score": 7, "answer_count": 2}],
            "YouTube": [{"id": "vid1", "url": "https://www.youtube.com/watch?v=vid1",
                         "title": "LLM agents explained", "description": "A walkthrough.",
                         "published_at": now, "like_count": 900, "comment_count": 80,
                         "channel": {"title": "Chan", "subscriber_count": 50000, "account_age_days": 2000}}],
        }
        signals = {s.source_platform: s for s in ingest.ingest(payload)}
        self.assertEqual(set(signals), set(payload))
        self.assertEqual(signals["Google Trends"].source_url, "https://www.cnbc.com/x")
        self.assertEqual(signals["Google Trends"].engagement_metrics.shares, 5000)
        self.assertGreaterEqual(signals["Bluesky"].author.account_age_days, 399)
        self.assertEqual(signals["Hugging Face"].engagement_metrics.likes, 30)
        self.assertEqual(signals["StackExchange"].author.follower_count, 900)
        self.assertEqual(signals["YouTube"].author.follower_count, 50000)
        for signal in signals.values():
            self.assertIsNone(gate1_quality.check_attribution(signal), signal.source_platform)

    def test_github_repository_normalises_as_independent_platform(self) -> None:
        signal = ingest.normalize_record({
            "id": 123,
            "full_name": "org/agent-benchmark",
            "url": "https://github.com/org/agent-benchmark",
            "description": "Benchmark suite for evaluating AI agents.",
            "topics": ["ai-agents", "benchmark"],
            "pushed_at": _iso_hours_ago(2),
            "stargazers_count": 120,
            "forks_count": 24,
            "open_issues_count": 8,
            "owner": "org",
        }, "GitHub")
        self.assertIsNotNone(signal)
        self.assertEqual(signal.source_platform, "GitHub")
        self.assertEqual(signal.source_url, "https://github.com/org/agent-benchmark")
        self.assertIn("ai-agents", signal.raw_content.body)
        self.assertEqual(signal.engagement_metrics.total, 152)
        self.assertIsNone(gate1_quality.check_attribution(signal))
        score, _ = gate1_quality.score_credibility(signal)
        self.assertGreaterEqual(score, config.MIN_CREDIBILITY_SCORE)

    def test_payload_merges_stackexchange_and_youtube_lookups(self) -> None:
        result = crawler.CrawlResult()
        result.put("StackExchange", "ai:5", {"site": "ai", "owner": {"user_id": 9}})
        result.put("YouTube", "v", {"channel": {"id": "c1"}})
        result.se_users["ai:9"] = int((datetime.now(timezone.utc) - timedelta(days=500)).timestamp())
        result.yt_channels["c1"] = {"created_at": _iso_hours_ago(24 * 900), "subscriber_count": 1234}
        payload = result.payload()
        self.assertGreaterEqual(payload["StackExchange"][0]["owner"]["account_age_days"], 499)
        self.assertEqual(payload["YouTube"][0]["channel"]["subscriber_count"], 1234)
        self.assertGreaterEqual(payload["YouTube"][0]["channel"]["account_age_days"], 899)

    def test_hacker_news_record(self) -> None:
        signal = ingest.normalize_record({
            "objectID": "4242", "title": "Open benchmark for LLM agents",
            "url": "https://arxiv.org/abs/2610.00001", "points": 220, "num_comments": 80,
            "created_at_i": int(datetime.now(timezone.utc).timestamp()) - 3600,
            "author": {"name": "researcher", "account_age_days": 2000, "karma": 9000},
        }, "Hacker News")
        self.assertEqual(signal.source_platform, "Hacker News")
        self.assertEqual(signal.engagement_metrics.likes, 220)
        self.assertEqual(signal.author.follower_count, 9000)
        self.assertEqual(signal.domain, "arxiv.org")
        self.assertLess(signal.age_hours, 2)

    def test_hacker_news_without_url_points_at_discussion(self) -> None:
        signal = ingest.normalize_record(
            {"objectID": "7", "title": "Ask HN: LLM evals?", "author": "x",
             "created_at_i": 1789181427}, "Hacker News")
        self.assertEqual(signal.source_url, "https://news.ycombinator.com/item?id=7")

    def test_mastodon_record_strips_html(self) -> None:
        signal = ingest.normalize_record({
            "id": "1", "url": "https://mastodon.social/@a/1", "created_at": _iso_hours_ago(1),
            "content": "<p>New <a href='#'>#LLM</a> benchmark &amp; results</p>",
            "favourites_count": 10, "reblogs_count": 4, "replies_count": 2,
            "account": {"acct": "a", "created_at": _iso_hours_ago(24 * 800), "followers_count": 900},
        }, "Mastodon")
        self.assertEqual(signal.raw_content.body, "New #LLM benchmark & results")
        self.assertEqual(signal.engagement_metrics.total, 16)
        self.assertGreaterEqual(signal.author.account_age_days, 799)

    def test_news_record_passes_attribution_and_credibility(self) -> None:
        signal = ingest.normalize_record({
            "url": "https://techcrunch.com/2026/10/16/story/", "title": "A model launch",
            "description": "A lab released an open-weight model with new benchmark results today.",
            "publisher": "TechCrunch", "published_at": _iso_hours_ago(3),
        }, "News")
        self.assertIsNone(gate1_quality.check_attribution(signal))
        score, _ = gate1_quality.score_credibility(signal)
        self.assertGreaterEqual(score, 40.0)


class _FakeGemini:
    online = True

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts = []

    def generate_text(self, prompt: str, label: str = "") -> str:
        self.prompts.append(prompt)
        return self.reply


class TestRelevanceGate(unittest.TestCase):
    def _payload(self):
        now = _iso_hours_ago(1)
        return {
            "News": [
                {"url": "https://a.example/1", "title": "Lab releases open-weight 30B model",
                 "description": "Benchmarks and weights published.", "published_at": now},
                {"url": "https://a.example/2", "title": "Local bakery wins award",
                 "description": "The owner says AI had nothing to do with it.", "published_at": now},
            ],
            "Bluesky": [
                {"uri": "at://x/1", "url": "https://bsky.app/profile/a/post/1", "created_at": now,
                 "text": "LLM is short for a joke", "author": {"handle": "a"}},
            ],
        }

    def test_parse_scores_tolerates_prose_and_clamps(self) -> None:
        from web_crawler import relevance

        self.assertEqual(relevance.parse_scores('Here you go: {"1": 95, "2": 12, "3": 140}', 3),
                         {1: 95, 2: 12, 3: 100})
        self.assertEqual(relevance.parse_scores('"1": 80, "2": 5', 2), {1: 80, 2: 5})
        self.assertEqual(relevance.parse_scores(None, 2), {})
        self.assertEqual(relevance.parse_scores('{"7": 50}', 2), {})

    def test_off_topic_records_are_dropped_and_scores_kept(self) -> None:
        from web_crawler import relevance

        fake = _FakeGemini('{"1": 92, "2": 15, "3": 20}')
        kept, dropped, stats = relevance.filter_payload(self._payload(), client=fake, minimum=50)
        self.assertEqual({p: len(r) for p, r in kept.items()}, {"News": 1})
        self.assertEqual(kept["News"][0]["ai_relevance"], 92)
        self.assertEqual(len(dropped), 2)
        self.assertEqual({d["platform"] for d in dropped}, {"News", "Bluesky"})
        self.assertEqual(stats["off-topic dropped"], 2)
        self.assertIn("1. [News] Lab releases open-weight 30B model", fake.prompts[0])

    def test_gate_fails_open_when_gemini_is_offline_or_answers_badly(self) -> None:
        from web_crawler import relevance

        offline = _FakeGemini("")
        offline.online = False
        kept, dropped, stats = relevance.filter_payload(self._payload(), client=offline)
        self.assertEqual(sum(len(r) for r in kept.values()), 3)
        self.assertEqual(stats["relevance unscored"], 3)
        kept, dropped, stats = relevance.filter_payload(self._payload(), client=_FakeGemini("nope"))
        self.assertEqual(sum(len(r) for r in kept.values()), 3)
        self.assertEqual(dropped, [])

    def test_crawl_result_buckets_shrink_after_the_gate(self) -> None:
        from unittest.mock import patch

        from web_crawler import relevance

        result = crawler.CrawlResult()
        payload = self._payload()
        for platform, records in payload.items():
            for index, record in enumerate(records):
                result.put(platform, "{}-{}".format(platform, index), record)
        with patch.object(relevance, "get_client", return_value=_FakeGemini('{"1": 90, "2": 10, "3": 10}')):
            crawler.apply_relevance_gate(result, minimum=50)
        self.assertEqual({p: len(r) for p, r in result.payload().items()}, {"News": 1})
        self.assertEqual(result.stats["News records"], 1)
        self.assertEqual(result.stats["Bluesky records"], 0)
        self.assertEqual(len(result.off_topic), 2)


class TestLinkExpansionPriority(unittest.TestCase):
    def test_primary_sources_rank_before_secondary_coverage(self) -> None:
        links = [
            "https://www.cnn.com/tech/ai-story",
            "https://arxiv.org/abs/2610.12345",
            "https://mit.edu/news/new-model",
            "https://someblog.example/post",
        ]
        ranked = sorted(links, key=lambda u: -crawler.expansion_priority(u))
        self.assertEqual(ranked[0], "https://arxiv.org/abs/2610.12345")
        self.assertEqual(ranked[1], "https://mit.edu/news/new-model")
        self.assertEqual(ranked[-1], "https://someblog.example/post")

    def test_aggregators_are_never_expanded(self) -> None:
        parent = "https://techcrunch.com/2026/10/25/story/"
        for url in ("https://www.techmeme.com/261025/p1", "https://flipboard.com/@x/topic",
                    "https://bsky.app/profile/a/post/1"):
            self.assertFalse(crawler.expandable_link(url, parent), url)

    def test_articles_keep_enough_text_for_the_creative_stage(self) -> None:
        self.assertGreaterEqual(crawler.settings.MAX_ARTICLE_CHARS, 6000)


if __name__ == "__main__":
    unittest.main()
