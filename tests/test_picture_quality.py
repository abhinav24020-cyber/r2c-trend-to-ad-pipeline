import json
import unittest
from unittest.mock import patch

from PIL import Image

from image_generator import generate_images, picture_quality
from trend_pipeline import config


class _Judge:
    """Gemini stand-in: scores pictures by their colour (red ones are better)."""
    online = True

    def judge_image(self, image, instructions):
        red = image.convert("RGB").getpixel((0, 0))[0]
        score = 9 if red > 128 else 4
        return json.dumps({"adherence": score, "realism": score, "catchiness": score, "artifacts": score,
                           "note": "red" if red > 128 else "blue"})


class TestPictureQuality(unittest.TestCase):
    def test_parse_scores_clamps_and_averages(self):
        scores = picture_quality.parse_scores('{"adherence": 12, "realism": 6, "catchiness": 8, "artifacts": 0}')
        self.assertEqual(scores["adherence"], 10.0)
        self.assertEqual(scores["artifacts"], 1.0)
        self.assertEqual(scores["overall"], round((10 + 6 + 8 + 1) / 4, 2))
        self.assertIsNone(picture_quality.parse_scores("not json"))
        self.assertIsNone(picture_quality.parse_scores('{"adherence": 5}'))

    def test_choose_best_picks_the_highest_score(self):
        images = [Image.new("RGB", (64, 64), (20, 20, 200)), Image.new("RGB", (64, 64), (220, 20, 20))]
        best, scores = picture_quality.choose_best(images, "a red poster", _Judge())
        self.assertEqual(best, 1)
        self.assertEqual(scores[1]["note"], "red")
        # Without Gemini the first picture is kept.
        self.assertEqual(picture_quality.choose_best(images, "x", None)[0], 0)

    def test_polish_keeps_size(self):
        image = Image.new("RGB", (120, 80), (100, 100, 100))
        self.assertEqual(picture_quality.polish(image).size, (120, 80))


class _TwoColourClient:
    """Cloudflare stand-in: first render blue, second render red."""
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    def text_to_image(self, **kwargs):
        _TwoColourClient.calls += 1
        colour = (20, 20, 200) if _TwoColourClient.calls % 2 else (220, 20, 20)
        return Image.new("RGB", (kwargs["width"], kwargs["height"]), colour)


class TestBestOfTwoEndpoint(unittest.TestCase):
    def test_generate_picture_returns_the_better_of_two(self):
        from app import app

        _TwoColourClient.calls = 0
        with patch.object(config, "CLOUDFLARE_ACCOUNT_ID", "acct"), patch.object(config, "CLOUDFLARE_API_TOKEN", "tok"), \
                patch.object(generate_images, "CloudflareImageClient", _TwoColourClient), \
                patch("trend_pipeline.gemini_client.get_client", return_value=_Judge()):
            response = app.test_client().post("/api/generate-picture", json={
                "prompt": "A researcher in a busy Delhi lab, real photo, no readable text anywhere.",
                "format": "1:1", "candidates": 2})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["candidates"], 2)
        self.assertEqual(body["scores"][body["chosen"]]["note"], "red")
        self.assertEqual(body["model"], "@cf/black-forest-labs/flux-2-klein-9b")  # 9B is the default now


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestVideoAds(unittest.TestCase):
    def test_render_video_writes_a_playable_mp4(self):
        import os
        import tempfile

        import imageio_ffmpeg

        from image_generator import video_ads

        picture = Image.new("RGB", (400, 600), (200, 80, 60))
        copy = {"eyebrow": "New model", "headline": "Five small models matched a giant",
                "subline": "R2C.Ai takes results like this to industry.", "cta": "Upload Your Research",
                "points": ["AI analysis of your paper or patent", "Commercial Readiness Score"], "sticker": "5 VS 1"}
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "ad.mp4")
            video_ads.render_video(picture, copy, out, "coral", "9:16", duration=1.0, fps=10)
            frames, seconds = imageio_ffmpeg.count_frames_and_secs(out)
            self.assertEqual(frames, 10)
            self.assertAlmostEqual(seconds, 1.0, places=1)

    def test_video_endpoint_returns_mp4_and_validates(self):
        import base64
        import io

        from app import app
        from image_generator import video_ads

        buffer = io.BytesIO()
        Image.new("RGB", (300, 400), (40, 120, 200)).save(buffer, "PNG")
        picture = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
        copy = {"headline": "Your research deserves a global stage", "cta": "Learn More"}
        client = app.test_client()
        real_render = video_ads.render_video

        def short_render(picture, copy, out, theme, ratio):  # the real renderer, 1 second long
            return real_render(picture, copy, out, theme, ratio, duration=1.0, fps=10)

        with patch.object(video_ads, "render_video", side_effect=short_render):
            response = client.post("/api/video", json={"picture": picture, "copy": copy, "theme": "night",
                                                       "format": "1.91:1"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body["video"].startswith("data:video/mp4;base64,"))
        self.assertEqual(body["format"], "9:16")  # a link-ad format becomes a vertical Reel
        self.assertEqual(client.post("/api/video", json={"picture": picture, "copy": {}, "theme": "night"}).status_code, 400)
        self.assertEqual(client.post("/api/video", json={"picture": picture, "copy": copy, "theme": "pink"}).status_code, 400)

