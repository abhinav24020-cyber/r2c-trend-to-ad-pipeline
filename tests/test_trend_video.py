"""Trend-specific videos (image_generator/trend_video.py) and their place in the daily run."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from image_generator import trend_video as tv  # noqa: E402
from prompt_generator.ad_creatives import ALLOWED_CTAS  # noqa: E402

TREND = {
    "trend_id": "trd_test_001",
    "topic_title": "Latent Introspection and INSEP Defense in LLMs",
    "topic_summary": "Researchers proposed INSEP, a defense against abliteration attacks.",
    "topic_key_facts": [
        "INSEP fine-tunes models to reduce the separability of harmful and benign activations.",
        "A Qwen 32B model demonstrates a latent capacity for introspection.",
        "Prompting the model increases sensitivity to injection from 0.3% to 39.9%.",
    ],
    "source_attribution": [{"platform": "Webpage", "source_url": "https://arxiv.org/abs/1",
                            "source_insight": "INSEP: Refusal-Direction Inseparability", "excerpt": "..."}],
}


def script(**over):
    base = {
        "scenes": [
            {"role": "HOOK", "on_screen": "Qwen 32B can introspect", "voiceover": "Qwen 32B notices injected ideas, from 0.3% to 39.9%."},
            {"role": "PROOF", "on_screen": "INSEP blocks abliteration", "voiceover": "INSEP keeps refusals intact after fine-tuning."},
            {"role": "BRIDGE", "on_screen": "Safety work like INSEP", "voiceover": "Work like INSEP matters; R2C.Ai at IIIT Delhi connects you with industry."},
            {"role": "CTA", "on_screen": "Model introspection researchers", "voiceover": "Researching model introspection? Upload your work to R2C.Ai."},
        ],
        "cta": ALLOWED_CTAS[0], "end_line": "Share your introspection research", "chips": ["a", "b", "c"],
        "tag": "LLM SAFETY", "audience_line": "For LLM safety researchers", "source_line": "Source: arXiv",
    }
    base.update(over)
    return base


class ScriptCheckTests(unittest.TestCase):
    def test_trend_specific_script_passes(self):
        check = tv.check_script(script(), TREND)
        self.assertTrue(check["pass"], check)
        self.assertIn("INSEP", check["named_terms_used"])
        self.assertIn("39.9%", check["named_terms_used"])

    def test_generic_script_fails(self):
        generic = script()
        generic["scenes"][0] = {"role": "HOOK", "on_screen": "AI is changing fast", "voiceover": "New AI research is everywhere."}
        check = tv.check_script(generic, TREND)
        self.assertFalse(check["pass"])
        self.assertEqual(check["scenes_without_trend_terms"], ["HOOK"])

    def test_generic_end_line_and_wrong_cta_fail(self):
        self.assertFalse(tv.check_script(script(end_line="Join us today"), TREND)["pass"])
        self.assertFalse(tv.check_script(script(cta="Buy now"), TREND)["pass"])

    def test_invented_numbers_fail(self):
        invented = script()
        invented["scenes"][1] = {"role": "PROOF", "on_screen": "INSEP doubles safety",
                                 "voiceover": "INSEP doubles refusal strength and cuts attacks by 80%."}
        check = tv.check_script(invented, TREND)
        self.assertFalse(check["pass"])
        self.assertIn("doubles", check["unsupported_claims"])
        self.assertIn("80%", check["unsupported_claims"])
        self.assertEqual(tv.check_script(script(), TREND)["unsupported_claims"], [])  # 0.3%, 39.9%, 32B are sourced

    def test_wrong_shape_fails(self):
        self.assertFalse(tv.check_script(script(chips=["only one"]), TREND)["shape_ok"])

    def test_answer_wrapped_in_a_list_is_read(self):
        self.assertEqual(tv._parse_json('[{"cta": "x"}]'), {"cta": "x"})
        self.assertEqual(tv._parse_json('text {"cta": "y"} more'), {"cta": "y"})


class TimingTests(unittest.TestCase):
    def test_scenes_follow_the_voice(self):
        words = [[{"w": "a", "t": 0.0, "d": 1.0}, {"w": "b", "t": 1.5, "d": 0.5}]] * 4
        tl = tv.timeline(words)
        self.assertEqual(tl["durs"], [2.6, 2.6, 2.6, 3.6])
        self.assertEqual(tl["starts"], [0, 2.6, 5.2, 7.8])
        self.assertAlmostEqual(tl["total"], 11.4)

    def test_ping_pong_plays_forward_then_back(self):
        self.assertEqual([tv.ping_pong(k, 4) for k in range(8)], [0, 1, 2, 3, 2, 1, 0, 1])
        self.assertEqual(tv.ping_pong(5, 1), 0)


class TwoAccountTests(unittest.TestCase):
    def setUp(self):
        tv._ACCOUNT["index"] = 0

    def tearDown(self):
        tv._ACCOUNT["index"] = 0

    def test_switches_to_the_second_account_at_the_daily_limit(self):
        from trend_pipeline import config
        used = []

        def call(account, token):
            used.append(account)
            if account == "acc1":
                raise RuntimeError("Cloudflare 429: you have used up your daily free allocation of 10,000 neurons")
            return "picture"

        with mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("acc1", "t1"), ("acc2", "t2")]):
            self.assertEqual(tv.cloudflare(call), "picture")
            self.assertEqual(tv.cloudflare(call), "picture")  # stays on account 2 for the rest of the run
        self.assertEqual(used, ["acc1", "acc2", "acc2"])

    def test_limit_on_the_last_account_stops_the_run(self):
        from trend_pipeline import config

        def call(account, token):
            raise RuntimeError("429 daily free allocation")

        with mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("acc1", "t1"), ("acc2", "t2")]):
            with self.assertRaises(RuntimeError):
                tv.cloudflare(call)
        self.assertEqual(tv._ACCOUNT["index"], 1)

    def test_other_errors_do_not_switch_accounts(self):
        from trend_pipeline import config

        def call(account, token):
            raise RuntimeError("500 server error")

        with mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("acc1", "t1"), ("acc2", "t2")]):
            with self.assertRaises(RuntimeError):
                tv.cloudflare(call)
        self.assertEqual(tv._ACCOUNT["index"], 0)

    def test_default_daily_ads_is_two_per_account(self):
        import daily_run
        from trend_pipeline import config
        with mock.patch.dict(os.environ, {"DAILY_MAX_ADS": "auto"}):
            with mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("a", "b"), ("c", "d")]):
                self.assertEqual(daily_run.default_max_ads(), 4)
            with mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("a", "b")]):
                self.assertEqual(daily_run.default_max_ads(), 2)
        with mock.patch.dict(os.environ, {"DAILY_MAX_ADS": "1"}):
            self.assertEqual(daily_run.default_max_ads(), 1)


class DailyRunVideoTests(unittest.TestCase):
    def setUp(self):
        import daily_run
        self.dr = daily_run
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs("web_data")
        os.makedirs(os.path.join("data", "output"))

    def tearDown(self):
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_crop_keeps_the_ad_size(self):
        from PIL import Image
        cropped = self.dr.crop_to(Image.new("RGB", (768, 1344)), "4:5")
        from trend_pipeline import config
        self.assertEqual(cropped.size, config.AD_ASPECT_SIZES["4:5"])

    def test_daily_limit_queues_the_unfinished_trends_for_tomorrow(self):
        from trend_pipeline import config
        trends = [{"topic_title": "INSEP blocks abliteration attacks", "trend_id": "t1", "composite_trend_score": 80},
                  {"topic_title": "VideoGym trains long video agents", "trend_id": "t2", "composite_trend_score": 79}]
        ranked = os.path.join("data", "output", "ranked.json")
        with open(ranked, "w", encoding="utf-8") as handle:
            json.dump(trends, handle)
        limit = RuntimeError("429: you have used up your daily free allocation of 10,000 neurons")
        with mock.patch.object(config, "RANKED_TRENDS_JSON", ranked), \
                mock.patch.object(config, "CLOUDFLARE_ACCOUNTS", [("a", "b")]), \
                mock.patch.object(self.dr, "TREND_VIDEOS", True), \
                mock.patch.object(self.dr, "make_ad_with_video", side_effect=limit), \
                mock.patch("trend_pipeline.gemini_client.get_client", return_value=mock.Mock(online=True)):
            summary = self.dr.run(skip_crawl=True, max_ads=2)
            queued = [t["topic_title"] for t in self.dr.load_queue()]
            # next day: the queue is served first
            done = mock.Mock(return_value={"id": "t1", "copy": {"headline": "H"}, "trend": trends[0],
                                           "video_kind": "trend"})
            with mock.patch.object(self.dr, "make_ad_with_video", done):
                second = self.dr.run(skip_crawl=True, max_ads=1)
        self.assertEqual(len(summary["ads_failed"]), 1)
        self.assertEqual(queued, [t["topic_title"] for t in trends])
        self.assertEqual(second["from_queue"], 1)
        self.assertEqual(done.call_args[0][0]["topic_title"], trends[0]["topic_title"])
        self.assertEqual(len(self.dr.load_queue()), 1)
        with open(self.dr.LAST_RUN_JSON, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["queue_left"], 1)

    def test_backup_schedule_skips_when_today_is_done(self):
        now = datetime(2026, 11, 25, 6, 0, tzinfo=timezone.utc)  # 11:30 IST
        self.dr.save_json(self.dr.LAST_RUN_JSON, {"finished_at": (now - timedelta(hours=3)).isoformat()})
        self.assertTrue(self.dr.ran_today(now))
        self.assertFalse(self.dr.ran_today(now + timedelta(days=1)))
        self.dr.save_json(self.dr.LAST_RUN_JSON, {"finished_at": now.isoformat(), "skipped_crawl": True})
        self.assertFalse(self.dr.ran_today(now))


class CronEndpointTests(unittest.TestCase):
    def setUp(self):
        import app as web_app
        self.client = web_app.app.test_client()

    def env(self, **values):
        base = {"CRON_SECRET": "c1", "GITHUB_TOKEN": "t", "GITHUB_REPO": "owner/repo"}
        base.update(values)
        return mock.patch.dict(os.environ, base)

    def test_wrong_secret_is_refused(self):
        with self.env():
            self.assertEqual(self.client.get("/api/cron/daily", headers={"Authorization": "Bearer nope"}).status_code, 401)
        with self.env(CRON_SECRET=""):
            self.assertEqual(self.client.get("/api/cron/daily", headers={"Authorization": "Bearer "}).status_code, 401)

    def test_starts_the_workflow(self):
        with self.env(), mock.patch("urllib.request.urlopen") as urlopen:
            r = self.client.get("/api/cron/daily", headers={"Authorization": "Bearer c1"})
        self.assertEqual(r.status_code, 200, r.get_json())
        sent = urlopen.call_args[0][0]
        self.assertTrue(sent.full_url.endswith("/repos/owner/repo/actions/workflows/daily.yml/dispatches"))
        self.assertEqual(json.loads(sent.data)["ref"], "main")


if __name__ == "__main__":
    unittest.main()
