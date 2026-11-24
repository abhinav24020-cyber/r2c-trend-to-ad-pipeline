"""Daily run helpers (scripts/daily_run.py) and the review flow in the web app."""
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import daily_run  # noqa: E402


class SameStoryTests(unittest.TestCase):
    def test_reworded_title_is_the_same_story(self):
        self.assertTrue(daily_run.is_same_story(
            "NVIDIA releases OpenShell 0.1 sandbox for AI agents",
            ["OpenShell 0.1 sandbox for AI agents released by NVIDIA"]))

    def test_different_story_is_new(self):
        self.assertFalse(daily_run.is_same_story(
            "Mistral Large 4 tops open model leaderboard",
            ["NVIDIA releases OpenShell 0.1 sandbox for AI agents"]))

    def test_pick_skips_made_and_repeated_and_caps(self):
        trends = [{"topic_title": "Qwen3.8 matches frontier coding models"},
                  {"topic_title": "Qwen3.8 matches frontier coding models today"},
                  {"topic_title": "Gemma 4 runs on phones"},
                  {"topic_title": "DeepSeek V4.1 Flash released"},
                  {"topic_title": "LTX 2.5 video model"}]
        picked = daily_run.pick_trends(trends, ["Gemma 4 runs on phones"], limit=2)
        self.assertEqual([t["topic_title"] for t in picked],
                         ["Qwen3.8 matches frontier coding models", "DeepSeek V4.1 Flash released"])

    def test_zero_limit_picks_nothing(self):
        self.assertEqual(daily_run.pick_trends([{"topic_title": "Gemma 4 runs on phones"}], [], limit=0), [])


class PruneTests(unittest.TestCase):
    def test_old_ads_and_their_files_are_removed(self):
        import tempfile
        now = datetime(2026, 11, 20, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(daily_run, "DAILY_DIR", tmp):
            os.makedirs(os.path.join(tmp, "old"))
            entries = [{"id": "new", "created_at": (now - timedelta(days=2)).isoformat()},
                       {"id": "old", "created_at": (now - timedelta(days=9)).isoformat()}]
            kept = daily_run.prune_old(entries, 7, now)
            self.assertEqual([e["id"] for e in kept], ["new"])
            self.assertFalse(os.path.exists(os.path.join(tmp, "old")))


class ReviewEndpointTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import app as web_app
        self.web_app = web_app
        self.tmp = tempfile.TemporaryDirectory()
        self.daily = os.path.join(self.tmp.name, "daily.json")
        self.reviews = os.path.join(self.tmp.name, "reviews.json")
        with open(self.daily, "w", encoding="utf-8") as handle:
            json.dump([{"id": "trd_1", "copy": {"headline": "H"}, "trend": {"topic_title": "T"}}], handle)
        self.patches = [mock.patch.object(web_app, "DAILY_JSON", self.daily),
                        mock.patch.object(web_app, "REVIEWS_JSON", self.reviews)]
        for p in self.patches:
            p.start()
        self.client = web_app.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def env(self, **extra):
        values = {"R2C_REVIEW_KEY": "s3cret"}
        values.update(extra)
        cleared = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "VERCEL")}
        cleared.update(values)
        return mock.patch.dict(os.environ, cleared, clear=True)

    def test_new_ads_are_pending(self):
        with self.env():
            data = self.client.get("/api/site-data").get_json()
        self.assertEqual(data["daily"][0]["status"], "pending")
        self.assertTrue(data["review_enabled"])

    def test_wrong_passcode_is_refused(self):
        with self.env():
            r = self.client.post("/api/review", json={"id": "trd_1", "decision": "approved", "key": "nope"})
        self.assertEqual(r.status_code, 403)

    def test_approve_then_site_shows_approved(self):
        with self.env():
            r = self.client.post("/api/review", json={"id": "trd_1", "decision": "approved", "key": "s3cret"})
            self.assertEqual(r.status_code, 200, r.get_json())
            data = self.client.get("/api/site-data").get_json()
        self.assertEqual(data["daily"][0]["status"], "approved")

    def test_unknown_ad_and_no_key_configured(self):
        with self.env():
            r = self.client.post("/api/review", json={"id": "x", "decision": "approved", "key": "s3cret"})
            self.assertEqual(r.status_code, 404)
        with self.env(R2C_REVIEW_KEY=""):
            r = self.client.post("/api/review", json={"id": "trd_1", "decision": "approved", "key": ""})
            self.assertEqual(r.status_code, 503)

    def test_vercel_without_github_token_does_not_pretend_to_save(self):
        with self.env(VERCEL="1"):
            r = self.client.post("/api/review", json={"id": "trd_1", "decision": "approved", "key": "s3cret"})
        self.assertEqual(r.status_code, 503)


class GeminiFallbackTests(unittest.TestCase):
    def test_overloaded_main_model_falls_back_to_the_next_model(self):
        from trend_pipeline import config
        from trend_pipeline.gemini_client import GeminiClient

        calls = []

        class Models:
            def generate_content(self, model, contents, **kwargs):
                calls.append(model)
                if model == config.GEMINI_TEXT_MODEL:
                    raise RuntimeError("503 UNAVAILABLE: high demand")
                return mock.Mock(text='{"ok": true}')

        client = GeminiClient.__new__(GeminiClient)
        client.online, client._client = True, mock.Mock(models=Models())
        client.calls_attempted = client.calls_failed = 0
        client._pace = lambda: None
        with mock.patch.object(config, "API_MAX_RETRIES", 2), mock.patch("time.sleep"),                 mock.patch.object(config, "GEMINI_FALLBACK_TEXT_MODELS", ["backup-model"]):
            text = client.generate_text("prompt", json_mode=True)
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(calls, [config.GEMINI_TEXT_MODEL, config.GEMINI_TEXT_MODEL, "backup-model"])


class FormatFallbackTests(unittest.TestCase):
    def test_failed_planned_format_uses_next_passing_format(self):
        records = [{"ad_type": "story", "status": "failed"},
                   {"ad_type": "engagement", "status": "success", "placement": "feed",
                    "post": {"headline": "H", "visual": {}}}]
        with mock.patch("prompt_generator.ad_creatives.generate_trend_creatives", return_value=records),                 mock.patch("prompt_generator.ad_creatives.reimagine_visual", side_effect=lambda r, *a, **k: r),                 mock.patch.object(daily_run, "make_picture", side_effect=RuntimeError("stop here")):
            with self.assertRaises(RuntimeError):
                daily_run.make_ad({"topic_title": "T"}, "9:16", "story", "solar", mock.Mock(),
                                  datetime(2026, 11, 20, tzinfo=timezone.utc))
            daily_run.make_picture.assert_called_once()
            self.assertEqual(daily_run.make_picture.call_args[0][1], "1:1")  # engagement = 1:1


class BuildWebDataPathTests(unittest.TestCase):
    """samples.json keeps Windows paths from the machine that made the ads; CI runs on Linux."""

    def test_foreign_path_maps_into_repo_and_missing_source_keeps_published_copy(self):
        import tempfile
        from PIL import Image
        from scripts import build_web_data as b
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd(); os.chdir(tmp)
            try:
                os.makedirs(os.path.join("data", "output", "samples"))
                Image.new("RGB", (8, 8)).save(os.path.join("data", "output", "samples", "a.png"))
                foreign = r"x:\Other\machine\repo\data\output\samples\a.png"
                self.assertEqual(b.repo_path(foreign), os.path.join("data", "output", "samples", "a.png"))
                dest = os.path.join("static", "showcase", "pictures", "t.jpg")
                self.assertEqual(b.save_jpg(foreign, dest), "/static/showcase/pictures/t.jpg")
                # source gone (as on CI): the already-published copy is kept
                self.assertEqual(b.save_jpg(r"x:\gone\b.png", dest), "/static/showcase/pictures/t.jpg")
                self.assertIsNone(b.copy_video(r"x:\gone\v.mp4", "t"))
                with self.assertRaises(FileNotFoundError):
                    b.save_jpg(r"x:\gone\c.png", os.path.join("static", "showcase", "pictures", "never.jpg"))
            finally:
                os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
