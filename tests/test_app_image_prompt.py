import base64
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import app as webapp
from app import app
from image_generator import generate_images
from trend_pipeline import config


class _FakeImage:
    def save(self, path: str, format: str) -> None:
        with open(path, "wb") as handle:
            handle.write(b"fake png")


class _FakeClient:
    model = "@cf/test-model"

    def text_to_image(self, **kwargs):
        self.request = kwargs
        return _FakeImage()


class _PictureClient:
    """Cloudflare stand-in that returns a real picture at the requested size."""

    def __init__(self, *args, **kwargs):
        self.model = args[2] if len(args) > 2 else "test"

    def text_to_image(self, **kwargs):
        _PictureClient.last = kwargs
        return Image.new("RGB", (kwargs["width"], kwargs["height"]), (40, 120, 200))


def _data_url(size=(300, 400), colour=(200, 60, 60)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _decode(data_url: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(data_url.split(",", 1)[1])))


COPY = {"eyebrow": "New benchmark", "headline": "Can your paper match industry giants?",
        "subline": "R2C.Ai bridges your research to industry partners.", "cta": "Upload Your Research",
        "points": ["AI analysis of your paper or patent", "Commercial Readiness Score"], "sticker": "5 vs 1"}


class TestSiteAndCompose(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()

    def test_home_page_and_site_data_load(self) -> None:
        for path in ("/", "/favicon.ico"):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)
        data = self.client.get("/api/site-data").get_json()
        for key in ("latest_trends", "showcase", "benchmark", "formats", "themes", "ctas", "benefits"):
            self.assertIn(key, data)
        self.assertTrue(data["showcase"], "the site ships with ready ads")
        first = data["showcase"][0]
        for path in (first["poster"], first["picture"]):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200, path)

    def test_compose_builds_each_format_from_a_showcase_picture(self) -> None:
        picture = self.client.get("/api/site-data").get_json()["showcase"][0]["picture"]
        for ratio, size in (("4:5", (896, 1120)), ("9:16", (768, 1344)), ("1:1", (1024, 1024)), ("1.91:1", (1216, 640))):
            response = self.client.post("/api/compose", json={"picture": picture, "format": ratio,
                                                              "theme": "coral", "copy": COPY})
            body = response.get_json()
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(_decode(body["image"]).size, size, ratio)

    def test_compose_accepts_a_generated_picture_as_data_url(self) -> None:
        response = self.client.post("/api/compose", json={"picture": _data_url(), "format": "1:1",
                                                          "theme": "night", "copy": COPY})
        self.assertEqual(response.status_code, 200, response.get_json())

    def test_compose_rejects_bad_input(self) -> None:
        good = {"picture": _data_url(), "format": "4:5", "theme": "solar", "copy": COPY}
        cases = [dict(good, theme="purple"), dict(good, format="2:1"), dict(good, copy=dict(COPY, headline=" ")),
                 dict(good, picture="/static/showcase/../../app.py"), dict(good, picture="/etc/passwd"),
                 dict(good, picture="data:image/png;base64,***")]
        for body in cases:
            self.assertEqual(self.client.post("/api/compose", json=body).status_code, 400, body)


class TestGenerationEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.client = app.test_client()

    def test_generate_picture_uses_the_chosen_model_and_format_size(self) -> None:
        with patch.object(config, "CLOUDFLARE_ACCOUNT_ID", "acct"), patch.object(config, "CLOUDFLARE_API_TOKEN", "tok"), \
                patch.object(generate_images, "CloudflareImageClient", _PictureClient):
            response = self.client.post("/api/generate-picture", json={
                "prompt": "A young researcher in a busy Delhi lab, real photo, no text anywhere.",
                "format": "9:16", "model": "klein9b"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["model"], "@cf/black-forest-labs/flux-2-klein-9b")
        self.assertEqual((_PictureClient.last["width"], _PictureClient.last["height"]), (768, 1344))
        self.assertEqual(_decode(body["picture"]).size, (768, 1344))

    def test_generate_picture_validates_input_and_configuration(self) -> None:
        self.assertEqual(self.client.post("/api/generate-picture", json={"prompt": "short"}).status_code, 400)
        long_prompt = "A young researcher in a busy Delhi lab, real photo, no text anywhere."
        self.assertEqual(self.client.post("/api/generate-picture",
                                          json={"prompt": long_prompt, "model": "dalle"}).status_code, 400)
        with patch.object(config, "CLOUDFLARE_ACCOUNT_ID", ""):
            self.assertEqual(self.client.post("/api/generate-picture", json={"prompt": long_prompt}).status_code, 503)

    def test_brief_returns_copy_and_a_themed_prompt(self) -> None:
        from tests.test_ad_creatives import FakeGemini, _good_brief, _trend

        fake = FakeGemini([json.dumps(_good_brief())])
        with patch("trend_pipeline.gemini_client.get_client", return_value=fake):
            response = self.client.post("/api/brief", json={"trend": _trend(), "format": "9:16", "theme": "lime"})
        body = response.get_json()
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(body["ad_type"], "story")
        self.assertEqual(body["copy"]["headline"], "Five small models matched a giant")
        self.assertIn("acid lime green", body["image_prompt"])

    def test_brief_and_scrape_validate_input(self) -> None:
        self.assertEqual(self.client.post("/api/brief", json={"trend": {}, "format": "4:5"}).status_code, 400)
        self.assertEqual(self.client.post("/api/scrape", json={"urls": ["not a link"]}).status_code, 400)


class TestLegacyGenerateAd(unittest.TestCase):
    def test_generate_ad_renders_at_the_selected_format_ratio(self) -> None:
        client = _FakeClient()
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(config, "GENERATED_ADS_DIR", temp_dir), \
                patch.object(generate_images, "create_image_client", return_value=client):
            response = app.test_client().post(
                "/api/generate-ad",
                json={"prompt": "Editorial illustration of a research lab at dawn, no text.",
                      "format": "Instagram"},
            )
            body = response.get_json()
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(body["aspect_ratio"], "4:5")
            self.assertEqual((client.request["width"], client.request["height"]),
                             config.AD_ASPECT_SIZES["4:5"])
            filename = body["image_url"].rsplit("/", 1)[-1]
            self.assertTrue(os.path.isfile(os.path.join(temp_dir, filename)))

    def test_generate_ad_rejects_an_empty_brief(self) -> None:
        response = app.test_client().post("/api/generate-ad", json={"prompt": "  "})
        self.assertEqual(response.status_code, 400)

    def test_generate_ad_reports_provider_errors(self) -> None:
        with patch.object(generate_images, "create_image_client",
                          side_effect=RuntimeError("needs CLOUDFLARE_ACCOUNT_ID")):
            response = app.test_client().post(
                "/api/generate-ad", json={"prompt": "Editorial illustration, no text, long enough."})
        self.assertEqual(response.status_code, 502)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
