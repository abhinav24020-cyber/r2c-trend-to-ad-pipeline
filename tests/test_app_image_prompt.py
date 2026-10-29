import os
import tempfile
import unittest
from unittest.mock import patch

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


class TestAppImagePrompt(unittest.TestCase):
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
