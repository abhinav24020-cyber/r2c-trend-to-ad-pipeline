"""Offline tests for Gemini prompts and the Cloudflare image stage."""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from typing import Any, Dict
from unittest.mock import patch

# Stage tests use mock clients and must not use developer API credentials.
os.environ["GEMINI_API_KEY"] = ""
from image_generator import generate_images
from prompt_generator import generate_prompts
from trend_pipeline import config


class FakeGemini:
    online = True

    def generate_text(self, prompt: str, label: str = "") -> str:
        self.last_request = prompt
        match = re.search(r"AIDA Stage: ([A-Z]+)", prompt)
        stage = match.group(1).lower() if match else "awareness"
        brand = " R2C.AI" if stage == "action" else ""
        post = {
            "aida_stage": stage,
            "short_version": (
                "A benchmark reveals where multi-agent tool use begins to plateau."
                + brand
            ),
            "long_version": (
                "A benchmark can reveal more than a single score. This research "
                "examines how autonomous systems perform across longer sequences "
                "of planning and tool use, and reports that errors can compound as "
                "the task continues. For researchers, this is a reminder to look "
                "beyond isolated tool accuracy when evaluating agent behavior. "
                "For commercialization teams, careful interpretation matters: "
                "benchmark evidence describes measured performance under stated "
                "conditions, not a guarantee of success in every product setting. "
                "Understanding the evaluation method, task limits, and reproducible "
                "evidence can help teams ask better questions about what is ready "
                "for further development and what still needs testing. "
                "The reported findings should be considered within the study's "
                "scope and do not, on their own, establish commercial readiness. "
                "What additional evidence would help your team assess this result?"
                + (" Explore R2C.AI and review one research profile." if stage == "action" else "")
            ),
            "hook": "What changes when agents must plan beyond a few steps?",
            "hashtags": ["AIResearch"],
            "target_persona": "Researchers and research commercialization professionals",
            "source_insight": "The supplied benchmark reports compounding errors on longer tasks.",
            "image_prompt": (
                "Create a polished editorial illustration about evaluating AI "
                "agents across long tool-use tasks. Show a precise sequence of "
                "small connected work surfaces receding into depth, with one "
                "subtle broken link near the far end as a metaphor for accumulated "
                "errors, not a literal performance chart. Use tactile graphite, "
                "matte glass, and restrained cyan light against a deep navy "
                "research-lab setting. Frame the scene from a slightly elevated "
                "three-quarter perspective, with the connected sequence leading "
                "the eye from lower left toward upper right. Use crisp focal "
                "detail, controlled depth of field, soft directional edge light, "
                "and balanced shadow. Keep generous uncluttered negative space "
                "in the upper-left quadrant for later typesetting. Sophisticated "
                "editorial art direction, realistic materials, clean geometry, "
                "high contrast, square social crop. No text, letters, labels, "
                "logos, charts, UI, watermark, or unsupported scientific claims."
            ),
        }
        return json.dumps(post)


class FakeImage:
    def save(self, path: str, format: str) -> None:
        with open(path, "wb") as handle:
            handle.write(b"fake png")


class FakeImageClient:
    def text_to_image(self, **kwargs: Any) -> FakeImage:
        self.request = kwargs
        return FakeImage()


class FailingImageClient:
    def text_to_image(self, **kwargs: Any) -> None:
        raise RuntimeError("inference provider unavailable")


def _trend() -> Dict[str, Any]:
    return {
        "trend_id": "trd_demo_001",
        "rank": 1,
        "topic_title": "A verified AI research breakthrough",
        "topic_summary": "Researchers report a reproducible benchmark result.",
        "topic_keywords": ["AI", "benchmark"],
        "cross_platform_validation": {"platforms": ["Reddit", "X"]},
    }


class TestGenerationStages(unittest.TestCase):
    def test_versioned_prompts_include_required_context_for_each_stage(self) -> None:
        for stage in generate_prompts.AIDA_STAGES:
            request = generate_prompts.make_post_request(_trend(), stage)
            self.assertIn("SYSTEM_PROMPT:", request)
            self.assertIn("DEVELOPER_PROMPT:", request)
            for variable in (
                "Research cluster:",
                "Cluster description:",
                "Evidence and source attribution:",
                "Target persona:",
                "Hook pattern:",
                "CTA pattern:",
                "Brand voice notes:",
            ):
                self.assertIn(variable, request)
            self.assertIn('"aida_stage": "{}"'.format(stage), request)

    def test_creative_angles_add_distinct_research_safe_prompt_direction(self) -> None:
        method_prompt = generate_prompts.make_post_request(
            _trend(), "interest", "Method spotlight"
        )
        comparison_prompt = generate_prompts.make_post_request(
            _trend(), "interest", "Evidence comparison"
        )
        self.assertIn("Selected angle: Method spotlight", method_prompt)
        self.assertIn("what it measures", method_prompt)
        self.assertIn("Selected angle: Evidence comparison", comparison_prompt)
        self.assertIn("fabricate a baseline", comparison_prompt)
        with self.assertRaisesRegex(ValueError, "Unsupported creative angle"):
            generate_prompts.make_post_request(_trend(), "interest", "Urgency")

    def test_posts_generated_for_all_aida_stages_obey_contract(self) -> None:
        client = FakeGemini()
        for stage in generate_prompts.AIDA_STAGES:
            result = generate_prompts.generate_post_record(_trend(), stage, client)
            self.assertEqual(result["status"], "success", result["error_message"])
            post = result["post"]
            self.assertEqual(set(post), generate_prompts.POST_FIELDS)
            self.assertEqual(post["aida_stage"], stage)
            self.assertLess(len(post["short_version"]), 280)
            self.assertGreaterEqual(len(post["long_version"]), 600)
            self.assertLessEqual(len(post["long_version"]), 1200)
            self.assertLessEqual(len(post["hashtags"]), 2)
        awareness = generate_prompts.generate_post_record(
            _trend(), "awareness", client
        )["post"]
        self.assertNotIn("r2c.ai", awareness["long_version"].casefold())
        action = generate_prompts.generate_post_record(_trend(), "action", client)["post"]
        self.assertIn("r2c.ai", action["long_version"].casefold())

    def test_invalid_model_json_is_reported_as_failure(self) -> None:
        class InvalidGemini:
            def generate_text(self, prompt: str, label: str = "") -> str:
                return "Here is your post: not JSON"

        result = generate_prompts.generate_post_record(
            _trend(), "awareness", InvalidGemini()
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("not valid JSON", result["error_message"])

    def test_action_requires_a_measurable_cta(self) -> None:
        action_request = generate_prompts.make_post_request(_trend(), "action")
        post = json.loads(FakeGemini().generate_text(action_request, ""))
        post["long_version"] = post["long_version"].replace(
            "Explore R2C.AI and review one research profile.",
            "Explore R2C.AI today.",
        )
        with self.assertRaisesRegex(ValueError, "measurable one-item"):
            generate_prompts.parse_and_validate_post(json.dumps(post), "action")

    def test_image_brief_must_be_detailed_enough_for_generation(self) -> None:
        post = json.loads(FakeGemini().generate_text("", ""))
        post["image_prompt"] = "A dark blue image about AI."
        with self.assertRaisesRegex(ValueError, "detailed 500-2400 character"):
            generate_prompts.parse_and_validate_post(
                json.dumps(post), "awareness"
            )

    def test_prompt_stage_writes_prompt_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = os.path.join(temp_dir, "trends.json")
            output_path = os.path.join(temp_dir, "prompts.json")
            with open(input_path, "w", encoding="utf-8") as handle:
                json.dump([_trend()], handle)
            with patch.object(config, "GEMINI_API_KEY", "test-gemini-key"):
                results = generate_prompts.run(
                    input_path,
                    output_path,
                    gemini_client=FakeGemini(),
                    stage="interest",
                )
            self.assertEqual(results[0]["status"], "success")
            with open(output_path, "r", encoding="utf-8") as handle:
                prompts = json.load(handle)
            self.assertEqual(prompts[0]["trend_id"], "trd_demo_001")
            self.assertEqual(prompts[0]["aida_stage"], "interest")
            self.assertEqual(
                set(prompts[0]["post"]), generate_prompts.POST_FIELDS
            )

    def test_image_stage_saves_image_and_manifest(self) -> None:
        prompt_record = generate_prompts.generate_post_record(
            _trend(), "action", FakeGemini()
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = os.path.join(temp_dir, "ads.json")
            images_dir = os.path.join(temp_dir, "images")
            image_client = FakeImageClient()
            results = generate_images.run(
                input_path=self._write_prompt(temp_dir, prompt_record),
                output_path=output_path,
                images_dir=images_dir,
                image_client=image_client,
            )
            self.assertEqual(results[0]["status"], "success")
            self.assertEqual(results[0]["image_provider"], "injected-client")
            self.assertEqual(
                image_client.request["num_inference_steps"], config.AD_IMAGE_STEPS
            )
            self.assertIn("watermark", image_client.request["negative_prompt"])
            self.assertEqual(image_client.request["guidance_scale"], 3.5)
            self.assertTrue(os.path.isfile(results[0]["image_path"]))
            self.assertEqual(results[0]["social_post"]["aida_stage"], "action")
            self.assertIsNone(results[0]["image_url"])
            with open(output_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest[0]["trend_id"], "trd_demo_001")

    def test_image_api_failure_is_reported(self) -> None:
        prompt_record = generate_prompts.generate_post_record(
            _trend(), "interest", FakeGemini()
        )
        result = generate_images.generate_image_record(
            prompt_record, FailingImageClient(), tempfile.gettempdir()
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("inference provider unavailable", result["error_message"])

    def test_image_stage_rejects_minimal_briefs_before_provider_call(self) -> None:
        prompt_record = generate_prompts.generate_post_record(
            _trend(), "action", FakeGemini()
        )
        prompt_record["post"]["image_prompt"] = "A blue image about AI."
        image_client = FakeImageClient()
        result = generate_images.generate_image_record(
            prompt_record, image_client, tempfile.gettempdir()
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("500-2400 character", result["error_message"])
        self.assertFalse(hasattr(image_client, "request"))

    def test_unknown_image_provider_is_rejected_with_setup_hint(self) -> None:
        with patch.object(config, "IMAGE_PROVIDER", "other"):
            with self.assertRaisesRegex(RuntimeError, "IMAGE_PROVIDER=cloudflare"):
                generate_images.create_image_client()

    @staticmethod
    def _write_prompt(temp_dir: str, prompt_record: Dict[str, Any]) -> str:
        path = os.path.join(temp_dir, "prompts.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump([prompt_record], handle)
        return path


class TestCloudflareImages(unittest.TestCase):
    """Cloudflare Workers AI client, with the HTTP call mocked (no network)."""

    @staticmethod
    def _png_b64() -> str:
        import base64
        import io

        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), (30, 90, 200)).save(buffer, format="JPEG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def _response(self, body: Dict[str, Any]):
        class _Resp:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self, *_args) -> bytes:
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_exc) -> None:
                return None

        return _Resp(json.dumps(body).encode("utf-8"))

    def test_cloudflare_requires_credentials(self) -> None:
        with patch.object(config, "IMAGE_PROVIDER", "cloudflare"), \
                patch.object(config, "CLOUDFLARE_ACCOUNT_ID", ""), \
                patch.object(config, "CLOUDFLARE_API_TOKEN", ""):
            with self.assertRaisesRegex(RuntimeError, "CLOUDFLARE_ACCOUNT_ID"):
                generate_images.create_image_client()

    def test_cloudflare_client_is_selected_and_clamps_steps(self) -> None:
        with patch.object(config, "IMAGE_PROVIDER", "cloudflare"), \
                patch.object(config, "CLOUDFLARE_ACCOUNT_ID", "acct123"), \
                patch.object(config, "CLOUDFLARE_API_TOKEN", "tok"), \
                patch.object(config, "CLOUDFLARE_IMAGE_STEPS", 30), \
                patch.object(config, "CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell"):
            client = generate_images.create_image_client()
        self.assertIsInstance(client, generate_images.CloudflareImageClient)
        self.assertEqual(client.steps, 8)
        self.assertFalse(client.supports_size)
        self.assertIn("/accounts/acct123/ai/run/@cf/black-forest-labs/flux-1-schnell", client.url)

    def test_flux2_gets_each_stage_aspect_ratio_as_multipart_fields(self) -> None:
        client = generate_images.CloudflareImageClient(
            "acct", "tok", "@cf/black-forest-labs/flux-2-klein-4b", 8)
        sent = []

        def fake_urlopen(request, timeout=None):
            sent.append((request.get_header("Content-type"), request.data.decode("utf-8")))
            return self._response({"result": {"image": self._png_b64()}})

        with patch.object(generate_images, "urlopen", fake_urlopen), \
                tempfile.TemporaryDirectory() as temp_dir:
            results = [
                generate_images.generate_image_record(
                    generate_prompts.generate_post_record(_trend(), stage, FakeGemini()),
                    client, temp_dir)
                for stage in generate_prompts.AIDA_STAGES
            ]
        self.assertEqual([r["aspect_ratio"] for r in results],
                         [config.AD_STAGE_ASPECT_RATIO[s] for s in generate_prompts.AIDA_STAGES])
        self.assertEqual(len({r["aspect_ratio"] for r in results}), 4)
        content_type, body = sent[-1]  # action stage -> 9:16
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        width, height = config.AD_ASPECT_SIZES["9:16"]
        self.assertIn('name="width"\r\n\r\n{}'.format(width), body)
        self.assertIn('name="height"\r\n\r\n{}'.format(height), body)
        self.assertNotIn('name="steps"', body)
        self.assertTrue(results[-1]["image_path"].endswith("_action_9x16.png"))

    def test_each_stage_uses_a_different_creative_angle_and_frame(self) -> None:
        records = [generate_prompts.generate_post_record(_trend(), stage, FakeGemini())
                   for stage in generate_prompts.AIDA_STAGES]
        self.assertEqual(len({r["creative_angle"] for r in records}), 4)
        request = generate_prompts.make_post_request(_trend(), "action")
        self.assertIn("rendered as a 9:16 tall vertical image", request)
        self.assertIn("Selected angle: Problem or research gap", request)
        self.assertIn("graphic poster", request)
        briefs = [generate_prompts.make_post_request(_trend(), s).split("IMAGE-GENERATION BRIEF")[1]
                  for s in generate_prompts.AIDA_STAGES]
        self.assertEqual(len(set(briefs)), 4)  # four stages, four visual treatments

    def test_prompt_is_trimmed_to_provider_limit_on_a_sentence(self) -> None:
        prompt = "A precise editorial scene. " * 120
        fitted = generate_images.fit_prompt(prompt)
        self.assertLessEqual(len(fitted), generate_images.CLOUDFLARE_MAX_PROMPT_CHARS)
        self.assertTrue(fitted.endswith("."))
        self.assertEqual(generate_images.fit_prompt("short  prompt"), "short prompt")

    def test_cloudflare_image_saved_per_stage_with_provider_metadata(self) -> None:
        client = generate_images.CloudflareImageClient("acct", "tok", "@cf/model", 8)
        sent = {}

        def fake_urlopen(request, timeout=None):
            sent["auth"] = request.get_header("Authorization")
            sent["body"] = json.loads(request.data)
            return self._response({"success": True, "result": {"image": self._png_b64()}})

        records = [generate_prompts.generate_post_record(_trend(), stage, FakeGemini())
                   for stage in ("interest", "action")]
        with patch.object(generate_images, "urlopen", fake_urlopen), \
                tempfile.TemporaryDirectory() as temp_dir:
            results = [generate_images.generate_image_record(r, client, temp_dir) for r in records]
            paths = [r["image_path"] for r in results]
            self.assertTrue(all(os.path.isfile(p) for p in paths))
        self.assertEqual([r["status"] for r in results], ["success", "success"])
        self.assertEqual(len(set(paths)), 2)  # stages no longer overwrite each other
        self.assertTrue(paths[1].endswith("_action_9x16.png"))
        self.assertEqual(results[0]["image_provider"], "cloudflare-workers-ai")
        self.assertEqual(results[0]["image_model"], "@cf/model")
        self.assertEqual(sent["auth"], "Bearer tok")
        # Only the two fields the live API accepts; it rejects seed/negative_prompt.
        self.assertEqual(set(sent["body"]), {"prompt", "steps"})
        self.assertEqual(sent["body"]["steps"], 8)

    def test_cloudflare_quota_error_is_reported_on_the_record(self) -> None:
        from urllib.error import HTTPError
        import io

        def quota_exceeded(request, timeout=None):
            body = io.BytesIO(json.dumps({"errors": [{"message": "neurons exhausted"}]}).encode())
            raise HTTPError(request.full_url, 429, "Too Many Requests", {}, body)

        client = generate_images.CloudflareImageClient("acct", "tok", "@cf/model", 8)
        record = generate_prompts.generate_post_record(_trend(), "action", FakeGemini())
        with patch.object(generate_images, "urlopen", quota_exceeded), \
                patch.object(generate_images.time, "sleep", lambda _s: None):
            result = generate_images.generate_image_record(record, client, tempfile.gettempdir())
        self.assertEqual(result["status"], "failed")
        self.assertIn("429", result["error_message"])
        self.assertIn("neurons exhausted", result["error_message"])
        self.assertNotIn("tok", result["error_message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
