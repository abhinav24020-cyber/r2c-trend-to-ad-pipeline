"""Tests for the local creative studio's request validation."""
from __future__ import annotations

import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from trend_pipeline import config
from web_ui import server


class TestWebUIRequestValidation(unittest.TestCase):
    def test_parse_urls_accepts_lines_and_trailing_commas(self) -> None:
        urls = server.parse_urls(
            [
                '"https://research.example/paper",',
                "https://journal.example/article",
                "",
            ]
        )
        self.assertEqual(
            urls,
            [
                "https://research.example/paper",
                "https://journal.example/article",
            ],
        )

    def test_parse_urls_rejects_empty_input_and_too_many_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one"):
            server.parse_urls(" \n ")
        with self.assertRaisesRegex(ValueError, "no more than"):
            server.parse_urls(["https://example.org/{}".format(i) for i in range(9)])

    def test_parse_queries_normalizes_and_rejects_too_many(self) -> None:
        self.assertEqual(
            server.parse_queries(["  LLM   research ", "llm research", "agents"]),
            ["LLM research", "agents"],
        )
        with self.assertRaisesRegex(ValueError, "no more than"):
            server.parse_queries(["topic {}".format(index) for index in range(13)])

    def test_automatic_discovery_runs_the_normal_trend_pipeline(self) -> None:
        payload = {
            "Webpage": [
                {
                    "title": "New AI research",
                    "url": "https://research.example/paper",
                    "body": "A fresh research finding.",
                }
            ]
        }
        crawl_result = SimpleNamespace(
            payload=lambda: payload,
            queries=["AI agents"],
            trending_queries=[("AI agents", "Google Trends")],
            stats={"requests finished": 12},
        )
        trend = {
            "trend_id": "trend_1",
            "topic_title": "New AI research",
            "topic_summary": "A research finding.",
            "composite_trend_score": 82,
            "cross_platform_validation": {"platforms": ["News", "Webpage"]},
            "supporting_signal_count": 2,
            "source_attribution": [],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(server, "RUNS_DIR", temp_dir):
                with patch(
                    "web_crawler.crawler.crawl", return_value=crawl_result
                ) as crawl:
                    with patch(
                        "trend_pipeline.main.run_pipeline", return_value=[trend]
                    ) as run_pipeline:
                        result = server.create_discovered_topics(
                            {"queries": ["AI agents"]}
                        )

        crawl.assert_called_once_with(["AI agents"], use_radar=True)
        self.assertFalse(run_pipeline.call_args.kwargs["user_submitted_url"])
        self.assertEqual(result["topics"][0]["title"], "New AI research")
        self.assertEqual(result["sources"][0]["platform"], "Webpage")
        self.assertEqual(result["discovery"]["stats"]["requests finished"], 12)

    def test_run_paths_are_isolated_and_restored(self) -> None:
        original = {
            "DATA_DIR": config.DATA_DIR,
            "RANKED_TRENDS_JSON": config.RANKED_TRENDS_JSON,
            "STORAGE_BACKEND": config.STORAGE_BACKEND,
        }
        with patch.object(server, "RUNS_DIR", "test-runs"):
            previous = server._configure_run("test-runs/example")
            try:
                self.assertEqual(config.DATA_DIR, "test-runs/example")
                self.assertEqual(config.STORAGE_BACKEND, "json")
                self.assertNotEqual(config.RANKED_TRENDS_JSON, original["RANKED_TRENDS_JSON"])
            finally:
                server._restore_config(previous)
        self.assertEqual(config.DATA_DIR, original["DATA_DIR"])
        self.assertEqual(config.RANKED_TRENDS_JSON, original["RANKED_TRENDS_JSON"])
        self.assertEqual(config.STORAGE_BACKEND, original["STORAGE_BACKEND"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
