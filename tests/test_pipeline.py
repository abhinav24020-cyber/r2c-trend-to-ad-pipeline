"""
test_pipeline.py — Week 1 acceptance tests.

Runs on the standard library only (unittest), no pytest required:

    python -m unittest discover -s tests -v

Coverage: every Gate 1 rejection path fires, the >= 2 platform hard rule holds,
the composite score matches the weighted formula, the 70.0 threshold and ranking
behave, and the Week 1 output contract is intact.
"""
from __future__ import annotations

import logging
import os
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

# Prevent the test suite from using credentials stored in a developer's .env file.
os.environ["GEMINI_API_KEY"] = ""

from trend_pipeline import (
    config,
    gate1_cross,
    gate1_quality,
    gate2_cluster,
    gate2_rank,
    gate2_score,
    ingest,
    sample_data,
    storage,
    text_utils,
)

logging.disable(logging.CRITICAL)  # keep test output readable

# `unittest discover -s tests` imports test modules top-level, so tests/__init__.py
# never runs and an earlier module (e.g. one importing app.py) may already have
# loaded config with the developer's .env key. Force offline mode explicitly so
# these tests never call the real Gemini API or spend its quota.
from trend_pipeline import gemini_client as _gemini_client  # noqa: E402

config.GEMINI_API_KEY = ""
_gemini_client._SHARED = None


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def _run_gate1(raw: Dict[str, List[Dict[str, Any]]]):
    """Gate 1 part A, then part B on Gate 2's semantic clusters (as main.py does)."""
    signals = ingest.ingest(raw)
    verdicts = gate1_quality.run_quality_checks(signals)
    survivors = gate1_quality.passing_verdicts(verdicts)
    records = [storage.build_validated_record(v) for v in survivors]
    clusters = gate2_cluster.build_clusters(records)
    validated, rejected, groups = gate1_cross.run_cross_platform_validation(survivors, clusters)
    return signals, verdicts, validated, rejected, groups


class FakeGemini:
    """Online-looking client with canned replies; never touches the network."""

    online = True

    def __init__(self, reply: str = "", vectors=None) -> None:
        self.reply = reply
        self.vectors = vectors
        self.prompts: List[str] = []

    def generate_text(self, prompt: str, label: str = "") -> str:
        self.prompts.append(prompt)
        return self.reply

    def score_0_100(self, prompt: str, label: str = "") -> float:
        return 50.0

    def embed_texts(self, texts):
        return [list(v) for v in self.vectors[: len(texts)]]


def _cluster(cluster_id: str, vectors, platform: str = "News", titles=None) -> gate2_cluster.TopicCluster:
    cluster = gate2_cluster.TopicCluster(cluster_id=cluster_id)
    for index, vector in enumerate(vectors):
        title = (titles or [None] * len(vectors))[index] or "{} story {}".format(cluster_id, index)
        cluster.add(
            {
                "signal_id": "{}_{}".format(cluster_id, index),
                "gate_1_metrics": {"credibility_score": 70.0, "freshness_score": 0.8},
                "content": {"raw_text": title, "title_or_summary": title, "keywords": []},
                "source_attribution": {
                    "primary_platform": platform,
                    "timestamp": _hours_ago(1),
                    "source_url": "https://example.org/{}/{}".format(cluster_id, index),
                    "author_id": "author",
                },
            },
            list(vector),
        )
    return cluster


class TestConfig(unittest.TestCase):
    def test_weights_sum_to_one(self) -> None:
        config.validate_config()
        self.assertAlmostEqual(sum(config.WEIGHTS.values()), 1.0, places=6)

    def test_prd_hard_rules(self) -> None:
        self.assertGreaterEqual(config.MIN_CROSS_PLATFORM_COUNT, 2)
        # Lowered from 70 to 60, then 50, as topics became one-story clusters (see config).
        self.assertEqual(config.MIN_TREND_SCORE_THRESHOLD, 50.0)


class TestIngest(unittest.TestCase):
    def test_all_platforms_normalise(self) -> None:
        signals = ingest.ingest(sample_data.build_sample_raw_data())
        self.assertEqual(len(signals), sample_data.sample_record_count())
        self.assertEqual(
            {s.source_platform for s in signals},
            {"Reddit", "X", "Instagram", "Facebook", "Google Search"},
        )

    def test_native_field_mapping(self) -> None:
        """Reddit `ups`/`selftext` and X `favorite_count`/`full_text` must map through."""
        signals = ingest.ingest(
            {
                "Reddit": [
                    {
                        "id": "r1", "url": "https://reddit.com/r/x/1", "title": "T",
                        "selftext": "body text here", "created_utc": _hours_ago(1),
                        "author": {"name": "a", "account_age_days": 400, "karma": 100},
                        "ups": 7, "num_comments": 3,
                    }
                ],
                "X": [
                    {
                        "tweet_id": "1", "url": "https://x.com/a/1", "full_text": "tweet body",
                        "created_at": _hours_ago(1),
                        "user": {"screen_name": "a", "account_age_days": 400,
                                 "followers_count": 100},
                        "favorite_count": 9, "retweet_count": 2, "reply_count": 1,
                    }
                ],
            }
        )
        reddit = next(s for s in signals if s.source_platform == "Reddit")
        x_post = next(s for s in signals if s.source_platform == "X")
        self.assertEqual(reddit.engagement_metrics.likes, 7)
        self.assertIn("body text here", reddit.text)
        self.assertEqual(x_post.engagement_metrics.likes, 9)
        self.assertEqual(x_post.engagement_metrics.shares, 2)

    def test_timestamp_forms(self) -> None:
        epoch = datetime.now(timezone.utc).timestamp()
        for value in (epoch, _hours_ago(3), "2026-09-01", "2026-09-01T10:00:00Z"):
            self.assertIsNotNone(ingest.parse_timestamp(value), value)
        self.assertIsNone(ingest.parse_timestamp("not a date"))

    def test_bad_record_does_not_abort_batch(self) -> None:
        signals = ingest.ingest({"Reddit": [None, {"id": "ok", "url": "u", "title": "t"}]})
        self.assertEqual(len(signals), 1)


class TestGate1Checks(unittest.TestCase):
    def test_every_rejection_path_fires(self) -> None:
        _s, verdicts, _v, _r, _g = _run_gate1(sample_data.build_sample_raw_data())
        counts = gate1_quality.rejection_summary(verdicts)
        for status in (
            gate1_quality.REJECT_SPAM,
            gate1_quality.REJECT_DUPLICATE,
            gate1_quality.REJECT_LOW_CREDIBILITY,
            gate1_quality.REJECT_STALE,
            gate1_quality.REJECT_UNATTRIBUTED,
            gate1_quality.REJECT_SINGLE_SOURCE,
        ):
            self.assertGreater(counts.get(status, 0), 0, "no signal rejected as " + status)

    def test_spam_keyword_and_hashtag_stuffing(self) -> None:
        spam = ingest.normalize_record(
            {
                "tweet_id": "s", "url": "https://x.com/s/1",
                "full_text": "Amazing stuff, click here for a limited offer on agents today!",
                "created_at": _hours_ago(1),
                "user": {"screen_name": "s", "account_age_days": 900, "followers_count": 500},
            },
            "X",
        )
        self.assertIsNotNone(gate1_quality.check_spam(spam))

    def test_attribution_required(self) -> None:
        missing = ingest.normalize_record(
            {"tweet_id": "n", "full_text": "a" * 60, "created_at": _hours_ago(1)}, "X"
        )
        self.assertIsNotNone(gate1_quality.check_attribution(missing))

    def test_freshness_decay_is_monotonic(self) -> None:
        weights = [gate1_quality.freshness_weight(h) for h in (0, 12, 24, 48, 72)]
        self.assertEqual(weights, sorted(weights, reverse=True))
        self.assertAlmostEqual(weights[0], 1.0, places=3)
        self.assertTrue(all(0.0 <= w <= 1.0 for w in weights))

    def test_stale_signal_rejected(self) -> None:
        old = ingest.normalize_record(
            {
                "tweet_id": "o", "url": "https://x.com/o/1",
                "full_text": "A substantive research observation about agent benchmarks here.",
                "created_at": _hours_ago(config.FRESHNESS_CUTOFF_HOURS + 10),
                "user": {"screen_name": "o", "account_age_days": 900, "followers_count": 5000},
            },
            "X",
        )
        problem, _weight, _age = gate1_quality.check_freshness(old)
        self.assertIsNotNone(problem)

    def test_credibility_rejects_new_low_engagement_account(self) -> None:
        weak = ingest.normalize_record(
            {
                "tweet_id": "w", "url": "https://x.com/w/1",
                "full_text": "Agent benchmarks are pointless and everyone is wasting their time.",
                "created_at": _hours_ago(2),
                "user": {"screen_name": "w", "account_age_days": 5, "followers_count": 40},
            },
            "X",
        )
        score, _breakdown = gate1_quality.score_credibility(weak)
        self.assertLess(score, config.MIN_CREDIBILITY_SCORE)

    def test_high_authority_domain_scores_well(self) -> None:
        arxiv = ingest.normalize_record(
            {
                "result_id": "g", "link": "https://arxiv.org/abs/1", "displayLink": "arxiv.org",
                "title": "A Benchmark", "snippet": "We introduce a benchmark for agents.",
                "date": _hours_ago(5), "backlinks": 120,
            },
            "Google Search",
        )
        score, breakdown = gate1_quality.score_credibility(arxiv)
        self.assertGreaterEqual(breakdown["domain_authority"], 90.0)
        self.assertGreaterEqual(score, config.MIN_CREDIBILITY_SCORE)

    def test_exact_and_near_duplicates_caught(self) -> None:
        base = "Researchers published a new multi agent planning benchmark with open traces."
        index = gate1_quality.DuplicateIndex(text_utils.build_idf([base]))
        index.add("first", base)
        self.assertEqual(index.find_exact(base), "first")
        near = index.find_near(base.replace("published", "have published"))
        self.assertIsNotNone(near)

    def test_resurfaced_history_signal_is_rechecked_and_can_pass(self) -> None:
        signal = ingest.normalize_record(
            {
                "result_id": "history-1",
                "link": "https://arxiv.org/abs/1234.5678",
                "displayLink": "arxiv.org",
                "title": "Researchers publish an open agent evaluation benchmark",
                "snippet": "The paper reports reproducible results for multi-step tool use.",
                "date": _hours_ago(5),
                "backlinks": 120,
            },
            "Google Search",
        )

        first = gate1_quality.run_quality_checks([signal])[0]
        repeated = gate1_quality.run_quality_checks(
            [signal], post_history=[(signal.signal_id, signal.text)]
        )[0]

        self.assertTrue(first.passed)
        self.assertTrue(repeated.passed)
        self.assertGreaterEqual(repeated.credibility_score, config.MIN_CREDIBILITY_SCORE)
        self.assertIsNotNone(repeated.age_hours)

    def test_unrelated_text_is_not_a_duplicate(self) -> None:
        index = gate1_quality.DuplicateIndex(text_utils.build_idf(["banana bread recipe"]))
        index.add("first", "A simple banana bread recipe with walnuts and cinnamon.")
        self.assertIsNone(
            index.find_near("Quantised open weight models now run on laptop neural engines.")
        )


class TestGate1CrossPlatform(unittest.TestCase):
    def test_two_platform_rule_enforced(self) -> None:
        _s, _v, validated, rejected, groups = _run_gate1(sample_data.build_sample_raw_data())
        for verdict in validated:
            platforms = getattr(verdict, "platforms_detected", [])
            self.assertGreaterEqual(len(platforms), config.MIN_CROSS_PLATFORM_COUNT)
            self.assertTrue(verdict.cross_platform_confirmed)
            self.assertTrue(verdict.corroboration_group.startswith("cls_"))
        self.assertTrue(rejected, "fixture lacks a single-source case")
        for verdict in rejected:
            self.assertEqual(verdict.status, gate1_quality.REJECT_SINGLE_SOURCE)
            self.assertIn("needs >= {}".format(config.MIN_CROSS_PLATFORM_COUNT), verdict.reason)
        self.assertTrue(all(g.confirmed for g in groups))

    def test_multi_platform_topic_stays_together(self) -> None:
        """The agent-benchmark story appears on 4 platforms and must group as one."""
        _s, _v, validated, _r, groups = _run_gate1(sample_data.build_sample_raw_data())
        agent_groups = [
            g for g in groups if any("agent" in kw for kw in g.keywords) and g.confirmed
        ]
        self.assertTrue(agent_groups)
        self.assertGreaterEqual(max(g.platform_count for g in agent_groups), 3)

    def test_grouping_is_order_independent(self) -> None:
        """Average-linkage grouping must not depend on the order signals arrive in."""
        raw = sample_data.build_sample_raw_data()
        _s, _v, validated_a, _r, groups_a = _run_gate1(raw)
        reversed_raw = {k: list(reversed(v)) for k, v in reversed(list(raw.items()))}
        _s2, _v2, validated_b, _r2, groups_b = _run_gate1(reversed_raw)
        self.assertEqual(len(validated_a), len(validated_b))
        # Gate 1 dedup keeps whichever duplicate arrives first, so the surviving
        # ids differ; the group structure (count, sizes, platforms) must not.
        shape = lambda groups: sorted((g.size, sorted(g.platforms)) for g in groups)  # noqa: E731
        self.assertEqual(shape(groups_a), shape(groups_b))

    def test_rule_is_decided_on_the_semantic_cluster(self) -> None:
        """Two platforms in one cluster pass; a lone-platform cluster is rejected, whatever its size."""
        raw = sample_data.build_sample_raw_data()
        signals = ingest.ingest(raw)
        verdicts = gate1_quality.run_quality_checks(signals)
        survivors = gate1_quality.passing_verdicts(verdicts)[:4]
        mixed = _cluster("cls_001", [[1.0, 0.0]] * 2)
        mixed.signals[0]["signal_id"] = survivors[0].signal.signal_id
        mixed.signals[1]["signal_id"] = survivors[1].signal.signal_id
        mixed.signals[1]["source_attribution"]["primary_platform"] = "Reddit"
        lonely = _cluster("cls_002", [[0.0, 1.0]] * 2, platform="Bluesky")
        lonely.signals[0]["signal_id"] = survivors[2].signal.signal_id
        lonely.signals[1]["signal_id"] = survivors[3].signal.signal_id

        validated, rejected, confirmed = gate1_cross.run_cross_platform_validation(
            survivors, [mixed, lonely]
        )
        self.assertEqual({v.signal.signal_id for v in validated},
                         {survivors[0].signal.signal_id, survivors[1].signal.signal_id})
        self.assertEqual(len(rejected), 2)
        self.assertEqual([c.cluster_id for c in confirmed], ["cls_001"])
        self.assertEqual(rejected[0].platforms_detected, ["Bluesky"])


class TestGate2Clustering(unittest.TestCase):
    def test_average_linkage_groups_a_story_and_keeps_strangers_apart(self) -> None:
        records = [
            {"signal_id": sid, "content": {"raw_text": sid, "keywords": [sid]},
             "source_attribution": {"primary_platform": "News"}}
            for sid in ("a1", "a2", "a3", "b1")
        ]
        vectors = [[1.0, 0.0], [0.98, 0.2], [0.95, 0.31], [0.0, 1.0]]
        clusters = gate2_cluster.cluster_signals(records, vectors, threshold=0.9)
        self.assertEqual([sorted(c.signal_ids) for c in clusters], [["a1", "a2", "a3"], ["b1"]])
        self.assertEqual([c.cluster_id for c in clusters], ["cls_001", "cls_002"])

    def test_clustering_is_order_independent(self) -> None:
        records = [
            {"signal_id": sid, "content": {"raw_text": sid, "keywords": []},
             "source_attribution": {"primary_platform": "News"}}
            for sid in ("p", "q", "r", "s", "t")
        ]
        vectors = [[1.0, 0.0], [0.97, 0.24], [0.1, 0.99], [0.0, 1.0], [0.6, 0.8]]
        forward = gate2_cluster.cluster_signals(records, vectors, threshold=0.9)
        backward = gate2_cluster.cluster_signals(records[::-1], vectors[::-1], threshold=0.9)
        partition = lambda cs: sorted(sorted(c.signal_ids) for c in cs)  # noqa: E731
        self.assertEqual(partition(forward), partition(backward))

    def test_same_story_merge_honours_model_only_within_cosine_floor(self) -> None:
        close_a = _cluster("cls_001", [[1.0, 0.0]], titles=["DeepSeek releases V4.1 Flash update"])
        close_b = _cluster("cls_002", [[0.9, 0.44]], platform="Hacker News",
                           titles=["DeepSeek V4.1 Flash 1017 is out"])
        far = _cluster("cls_003", [[0.0, 1.0]], titles=["Meta releases Muse Spark open model"])
        fake = FakeGemini(reply='Sure: [["C0", "C1"], ["C1", "C2"]]')
        merged = gate2_cluster.merge_same_story_clusters([close_a, close_b, far], client=fake,
                                                         candidate_cosine=0.6)
        self.assertEqual(len(merged), 2)
        self.assertEqual(sorted(merged[0].signal_ids), ["cls_001_0", "cls_002_0"])
        self.assertEqual(merged[0].platforms, ["News", "Hacker News"])
        self.assertIn("C0", fake.prompts[0])
        self.assertNotIn("C2:", fake.prompts[0])  # the far cluster was never a candidate

    def test_same_story_merge_is_a_no_op_offline_or_on_bad_output(self) -> None:
        a = _cluster("cls_001", [[1.0, 0.0]])
        b = _cluster("cls_002", [[0.9, 0.44]])
        offline = FakeGemini(reply='[["C0","C1"]]')
        offline.online = False
        self.assertEqual(len(gate2_cluster.merge_same_story_clusters([a, b], client=offline)), 2)
        garbage = FakeGemini(reply="I cannot decide.")
        self.assertEqual(len(gate2_cluster.merge_same_story_clusters([a, b], client=garbage,
                                                                      candidate_cosine=0.6)), 2)
        self.assertEqual(gate2_cluster._parse_id_groups('[["C0"], ["C9","C1"], "x"]', {"C0", "C1"}), [])

    def test_entities_prefer_names_over_sentence_starts(self) -> None:
        cluster = _cluster("cls_001", [[1.0, 0.0]] * 3, titles=[
            "China's Moonshot AI launches Kimi K3.1 Pro with open weights",
            "Moonshot AI Releases Kimi K3.1 Pro: A 2.8T Total Params Model",
            "Five reasons Kimi K3.1 Pro matters",
        ])
        entities = gate2_cluster.extract_entities(cluster)
        self.assertEqual(entities[0], "Kimi K3.1 Pro")
        self.assertIn("Moonshot AI", entities)
        self.assertNotIn("Five", entities)

    def test_entities_break_on_verbs_and_connectors(self) -> None:
        """Real headlines from the 25 Oct crawl that produced junk entities before."""
        cluster = _cluster("cls_001", [[1.0, 0.0]] * 3, titles=[
            "Five Qwen3.8-27B Models Match Claude Fable 5 Using GVS5H Harness",
            "GitHub - slee-persis/GVS5H: GVS5H: Five Qwen3.8-27B Models Match Claude Fable 5",
            "Qwen3.8-27B Defiance: An Uncensored AI Model With 40 Modes",
        ])
        entities = gate2_cluster.extract_entities(cluster)
        self.assertIn("Qwen3.8-27B", entities[:3])
        self.assertIn("Claude Fable 5", entities)
        self.assertIn("GVS5H", entities)
        for junk in ("Fable 5 Using GVS5H", "AI Model With 40", "40 Modes", "GVS5H GVS5H", "Five"):
            self.assertNotIn(junk, entities)

    def test_summary_reply_parsing_includes_key_facts(self) -> None:
        reply = (
            "TITLE: Moonshot AI Releases Kimi K3.1 Pro\n"
            "SUMMARY: Moonshot released Kimi K3.1 Pro. It has 104B active parameters.\n"
            "KEY_FACTS:\n- 2.8T total / 104B active parameters [News]\n- Weights to be released [Bluesky]\n"
        )
        title, summary, facts = gate2_cluster._parse_summary_reply(reply)
        self.assertEqual(title, "Moonshot AI Releases Kimi K3.1 Pro")
        self.assertTrue(summary.startswith("Moonshot released"))
        self.assertEqual(len(facts), 2)
        self.assertEqual(gate2_cluster._parse_summary_reply(None), ("", "", []))

    def test_threshold_follows_the_vectors_actually_returned(self) -> None:
        """A quota error mid-run yields lexical vectors; they need the lexical threshold."""
        client = FakeGemini()
        client.embedding_mode = "gemini"
        self.assertEqual(gate2_cluster.similarity_threshold(client), config.CLUSTER_SIMILARITY_THRESHOLD)
        client.embedding_mode = "fallback"
        self.assertEqual(gate2_cluster.similarity_threshold(client),
                         config.CLUSTER_SIMILARITY_THRESHOLD_LEXICAL)
        self.assertEqual(gate2_cluster.merge_candidate_cosine(client),
                         config.CLUSTER_MERGE_CANDIDATE_COSINE_LEXICAL)

    def test_quota_errors_wait_for_the_rate_window(self) -> None:
        from trend_pipeline.gemini_client import quota_retry_seconds

        hinted = RuntimeError("429 RESOURCE_EXHAUSTED ... Please retry in 9.2s.")
        self.assertAlmostEqual(quota_retry_seconds(hinted), 10.2)
        self.assertEqual(quota_retry_seconds(RuntimeError("429 Too Many Requests")), 60.0)
        self.assertIsNone(quota_retry_seconds(RuntimeError("500 internal error")))

    def test_embedding_uses_the_lead_only(self) -> None:
        record = {"content": {"raw_text": "Title line\n" + "body " * 2000}}
        self.assertLessEqual(len(gate2_cluster._embedding_text(record)), config.CLUSTER_EMBED_CHARS)
        self.assertTrue(gate2_cluster._embedding_text(record).startswith("Title line"))


class TestGate2Scoring(unittest.TestCase):
    def setUp(self) -> None:
        _s, _v, validated, _r, _g = _run_gate1(sample_data.build_sample_raw_data())
        self.records = [storage.build_validated_record(v) for v in validated]
        self.clusters = gate2_cluster.run_clustering(self.records)

    def test_clusters_match_the_seeded_topics(self) -> None:
        self.assertGreaterEqual(len(self.clusters), 2)
        for cluster in self.clusters:
            self.assertGreaterEqual(cluster.size, config.MIN_CLUSTER_SIZE)
            self.assertGreaterEqual(len(cluster.platforms), config.MIN_CROSS_PLATFORM_COUNT)
            self.assertTrue(cluster.topic_title)
            self.assertTrue(cluster.topic_summary)

    def test_a_signal_appears_in_exactly_one_cluster(self) -> None:
        assigned = [sid for cluster in self.clusters for sid in cluster.signal_ids]
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_similar_signal_chain_does_not_merge_distinct_endpoints(self) -> None:
        records = [
            {
                "signal_id": signal_id,
                "content": {"raw_text": signal_id, "keywords": [signal_id]},
                "source_attribution": {"primary_platform": "Webpage"},
            }
            for signal_id in ("left", "bridge", "right")
        ]
        vectors = [
            [1.0, 0.0],
            [0.8, 0.6],
            [0.28, 0.96],
        ]

        clusters = gate2_cluster.cluster_signals(records, vectors, threshold=0.75)

        self.assertEqual(len(clusters), 2)
        self.assertFalse(
            any({"left", "right"}.issubset(set(cluster.signal_ids)) for cluster in clusters)
        )

    def test_cross_platform_ladder(self) -> None:
        cluster = self.clusters[0]
        for count, expected in ((2, 80.0), (3, 90.0), (4, 100.0), (5, 100.0)):
            cluster.signals = [
                {
                    "signal_id": "s{}".format(i),
                    "gate_1_metrics": {"credibility_score": 80.0, "freshness_score": 0.9},
                    "content": {"raw_text": "x", "keywords": []},
                    "source_attribution": {
                        "primary_platform": config.SUPPORTED_PLATFORMS[i],
                        "timestamp": _hours_ago(1),
                    },
                }
                for i in range(count)
            ]
            self.assertEqual(gate2_score.score_cross_platform(cluster), expected)

    def test_composite_matches_weighted_formula(self) -> None:
        for score in gate2_score.score_clusters(self.clusters):
            expected = (
                score.credibility_score * config.WEIGHTS["w_credibility"]
                + score.growth_rate_score * config.WEIGHTS["w_growth_rate"]
                + score.recency_score * config.WEIGHTS["w_recency"]
                + score.cross_platform_score * config.WEIGHTS["w_cross_platform"]
                + score.research_relevance_score * config.WEIGHTS["w_research"]
                + score.r2c_relevance_score * config.WEIGHTS["w_r2c"]
            )
            self.assertAlmostEqual(score.composite_trend_score, round(expected, 2), places=2)
            self.assertTrue(0.0 <= score.composite_trend_score <= 100.0)

    def test_all_subscores_in_range(self) -> None:
        for score in gate2_score.score_clusters(self.clusters):
            for name, value in score.breakdown().items():
                self.assertTrue(0.0 <= value <= 100.0, "{}={}".format(name, value))

    def test_growth_weights_mentions_by_platform(self) -> None:
        """Four Bluesky posts count for less velocity than two news articles."""
        social = _cluster("cls_001", [[1.0, 0.0]] * 4, platform="Bluesky")
        press = _cluster("cls_002", [[1.0, 0.0]] * 2, platform="News")
        social_score, social_evidence = gate2_score.score_growth_rate(social)
        press_score, press_evidence = gate2_score.score_growth_rate(press)
        self.assertEqual(social_evidence["recent_mentions"], 4)
        self.assertAlmostEqual(social_evidence["recent_weighted"],
                               4 * config.PLATFORM_SIGNAL_WEIGHT["Bluesky"])
        self.assertAlmostEqual(press_evidence["recent_weighted"],
                               2 * config.PLATFORM_SIGNAL_WEIGHT["News"])
        self.assertGreater(press_score, social_score)


class TestGate2Ranking(unittest.TestCase):
    def setUp(self) -> None:
        _s, _v, validated, _r, _g = _run_gate1(sample_data.build_sample_raw_data())
        records = [storage.build_validated_record(v) for v in validated]
        clusters = gate2_cluster.run_clustering(records)
        self.scores = gate2_score.score_clusters(clusters)

    def test_threshold_and_descending_rank(self) -> None:
        trends, dropped = gate2_rank.rank_trends(self.scores)
        for trend in trends:
            self.assertGreaterEqual(
                trend["composite_trend_score"], config.MIN_TREND_SCORE_THRESHOLD
            )
        for item in dropped:
            if item["status"] == gate2_rank.STATUS_BELOW_THRESHOLD:
                self.assertLess(item["composite_trend_score"], config.MIN_TREND_SCORE_THRESHOLD)
            else:
                self.assertEqual(item["status"], gate2_rank.STATUS_OFF_BRAND)
                self.assertLess(item["scoring_breakdown"]["r2c_relevance_score"],
                                config.MIN_R2C_RELEVANCE_SCORE)
        values = [t["composite_trend_score"] for t in trends]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual([t["rank"] for t in trends], list(range(1, len(trends) + 1)))

    def test_output_contract(self) -> None:
        trends, _dropped = gate2_rank.rank_trends(self.scores)
        self.assertTrue(trends, "fixture should yield at least one qualifying trend")
        required = {
            "trend_id", "rank", "topic_title", "topic_summary", "composite_trend_score",
            "minimum_threshold_applied", "scoring_breakdown", "cross_platform_validation",
            "supporting_signal_ids", "status",
        }
        for trend in trends:
            self.assertTrue(required.issubset(trend.keys()), required - set(trend.keys()))
            self.assertEqual(trend["status"], gate2_rank.STATUS_QUALIFIED)
            self.assertTrue(trend["cross_platform_validation"]["meets_minimum_rule"])
            self.assertEqual(len(set(trend["supporting_signal_ids"])),
                             trend["supporting_signal_count"])

    def test_trend_ids_are_unique(self) -> None:
        trends, _dropped = gate2_rank.rank_trends(self.scores)
        self.assertEqual(len({t["trend_id"] for t in trends}), len(trends))

    def test_trend_record_carries_evidence_for_content_generation(self) -> None:
        trends, _dropped = gate2_rank.rank_trends(self.scores)
        for trend in trends:
            self.assertIn("topic_entities", trend)
            self.assertIn("topic_key_facts", trend)
            for source in trend["source_attribution"]:
                self.assertTrue(source["source_insight"])
                self.assertTrue(source["excerpt"])
                self.assertLessEqual(len(source["excerpt"]), gate2_rank.SOURCE_EXCERPT_CHARS)

    def test_brand_fit_gate_drops_off_brand_topics(self) -> None:
        score = gate2_score.TrendScore(cluster=_cluster("cls_009", [[1.0, 0.0]] * 2))
        score.cluster.topic_title = "Celebrity uses a chatbot"
        score.composite_trend_score = 80.0
        score.r2c_relevance_score = config.MIN_R2C_RELEVANCE_SCORE - 1
        trends, dropped = gate2_rank.rank_trends([score])
        self.assertEqual(trends, [])
        self.assertEqual(dropped[0]["status"], gate2_rank.STATUS_OFF_BRAND)
        self.assertIn("R2C relevance", dropped[0]["drop_reason"])
        # A user-submitted URL disables the gate (minimum_r2c=0.0).
        trends, dropped = gate2_rank.rank_trends([score], minimum_score=0.0, minimum_r2c=0.0)
        self.assertEqual(len(trends), 1)
        below = gate2_score.TrendScore(cluster=_cluster("cls_010", [[1.0, 0.0]] * 2))
        below.composite_trend_score, below.r2c_relevance_score = 10.0, 90.0
        _t, dropped = gate2_rank.rank_trends([below])
        self.assertEqual(dropped[0]["status"], gate2_rank.STATUS_BELOW_THRESHOLD)


class TestScopeBoundary(unittest.TestCase):
    """The trend pipeline stays separate from prompt and image generation."""

    WEEK2_FUNCTION_MARKERS = (
        "generate_variant", "generate_content", "generate_post", "evaluate_post",
        "quality_score", "decide", "decision", "auto_publish", "regenerate", "publish",
    )

    def _module_paths(self):
        import pathlib

        project_root = pathlib.Path(__file__).resolve().parent.parent
        return sorted((project_root / "trend_pipeline").glob("*.py"))

    def test_no_week2_functions_defined(self) -> None:
        import ast

        for path in self._module_paths():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            defined = {
                node.name.lower()
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
            for marker in self.WEEK2_FUNCTION_MARKERS:
                offenders = [name for name in defined if marker in name]
                self.assertFalse(
                    offenders,
                    "{} defines Week 2 functionality: {}".format(path.name, offenders),
                )

    def test_expected_week1_modules_present(self) -> None:
        names = {p.name for p in self._module_paths()}
        for required in (
            "config.py", "ingest.py", "gate1_quality.py", "gate1_cross.py", "storage.py",
            "gate2_cluster.py", "gate2_score.py", "gate2_rank.py", "main.py",
        ):
            self.assertIn(required, names)

    def test_pipeline_output_is_a_handoff_not_a_post(self) -> None:
        """Week 1's terminal artefact is a ranked trend, never post content."""
        _s, _v, validated, _r, _g = _run_gate1(sample_data.build_sample_raw_data())
        records = [storage.build_validated_record(v) for v in validated]
        clusters = gate2_cluster.run_clustering(records)
        trends, _dropped = gate2_rank.rank_trends(gate2_score.score_clusters(clusters))
        for trend in trends:
            self.assertNotIn("variants", trend)
            self.assertNotIn("post_text", trend)
            self.assertNotIn("decision", trend)
            self.assertEqual(trend["status"], "QUALIFIED_FOR_WEEK_2_GENERATION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
