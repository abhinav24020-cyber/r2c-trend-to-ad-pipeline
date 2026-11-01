"""Offline tests for the Meta ad creative briefs (no API calls)."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from image_generator import generate_images
from prompt_generator import ad_creatives
from trend_pipeline import config


def _trend():
    return {
        "trend_id": "trd_1", "rank": 1,
        "topic_title": "GVS5H harness lets five Qwen3.8-27B models match Claude Fable 5",
        "topic_summary": "A training-free harness coordinates five small open models.",
        "topic_key_facts": ["Five Qwen3.8-27B models match Fable 5 on LiveCodeBench Hard [Webpage]"],
        "topic_entities": ["Qwen3.8-27B", "GVS5H", "Claude Fable 5"],
        "source_attribution": [{"platform": "Webpage", "source_url": "https://x.example",
                                "excerpt": "Five Qwen3.8-27B models match Claude Fable 5 on a single GPU."}],
    }


def _good_brief():
    visuals = {
        "hook": ("Five small identical rowing boats with matching orange oars", "overtaking a huge ocean liner on a glassy harbour",
                 "The little boats are in front and the liner is falling behind", "low-angle close-up, 35mm, shallow depth of field",
                 "hard dawn backlight with long shadows", "burnt orange, deep navy, cream"),
        "explainer": ("A miniature isometric control tower with five small radar dishes", "relaying signals along cables into one shared map table",
                      "Tiny figurines at the map table mark one finished route", "top-down isometric view",
                      "soft overhead studio light", "mint green, coral, white"),
        "engagement": ("A team of five tiny confident ants and one huge sleepy bear", "lined up at opposite ends of a tug-of-war rope",
                       "The ants are winning and the bear looks surprised", "straight-on medium shot",
                       "flat even light", "sunflower yellow, cobalt blue, pink"),
        "story": ("A relay team of five swimmers in matching caps", "diving into a night pool one after another",
                  "Splashes frozen in mid-air around them", "low vertical tracking shot, 24mm",
                  "stadium floodlight backlight", "electric purple, silver, black"),
        "conversion": ("A young researcher in a lab coat and an industry engineer", "testing a laptop demo of the five-model harness together at a demo table",
                       "Both are mid-laugh at the moment of hand-off", "eye-level candid shot, 50mm",
                       "warm window light", "warm beige, forest green, white"),
    }
    creatives = []
    for ad_type in ad_creatives.AD_TYPES:
        subject, action, stopper, camera, lighting, colors = visuals[ad_type]
        creatives.append({
            "ad_type": ad_type, "audience": "researchers",
            "eyebrow": "Open model breakthrough",
            "headline": "Five small models matched a giant",
            "points": ["AI analysis of your paper or patent", "Commercial Readiness Score",
                       "Matched with industry partners"],
            "subline": "Coordination, not size, closed the gap. R2C.Ai takes results like this to industry.",
            "cta": "Get Started Free",
            "caption": ("Five Qwen3.8-27B models matched Claude Fable 5 on LiveCodeBench Hard using the "
                        "GVS5H harness on a single GPU. What could your research do with the right "
                        "partner? Get Started Free on R2C.Ai."),
            "hashtags": ["AIResearch", "OpenSource", "R2CAi"],
            "fact_used": "Five Qwen3.8-27B models match Fable 5 on LiveCodeBench Hard",
            "visual": {"concept": "Small parts working together beat one big part.",
                       "focal_subject": subject, "action": action, "scroll_stopper": stopper,
                       "setting": "an open landscape", "camera": camera, "lighting": lighting,
                       "colors": colors, "mood": "bold, playful"},
        })
    return {"creatives": creatives}


class FakeGemini:
    online = True

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def generate_text(self, prompt, label=""):
        self.prompts.append(prompt)
        return self.replies.pop(0)


class TestAdCreatives(unittest.TestCase):
    def test_brief_request_carries_brand_trend_and_formats(self):
        request = ad_creatives.make_brief_request(_trend())
        self.assertIn("R2C.Ai", request)
        self.assertIn("IIIT Delhi", request)
        self.assertIn("GVS5H", request)
        self.assertIn("LiveCodeBench Hard", request)
        for ad_type, fmt in ad_creatives.AD_FORMATS.items():
            self.assertIn(ad_type, request)
            self.assertIn(fmt["aspect_ratio"], request)
        for cta in ad_creatives.ALLOWED_CTAS:
            self.assertIn(cta, request)

    def test_valid_brief_gives_five_creatives_with_their_own_ratios(self):
        client = FakeGemini([json.dumps(_good_brief())])
        records = ad_creatives.generate_trend_creatives(_trend(), client)
        self.assertEqual([r["ad_type"] for r in records], ad_creatives.AD_TYPES)
        self.assertTrue(all(r["status"] == "success" for r in records), [r["error_message"] for r in records])
        self.assertEqual([r["aspect_ratio"] for r in records], ["4:5", "3:4", "1:1", "9:16", "1.91:1"])
        self.assertEqual(len(client.prompts), 1)

    def test_image_prompt_is_composed_from_templates(self):
        records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))
        story = records[3]["background_prompt"]
        self.assertIn("Main subject centred and whole", story)
        self.assertIn("not CGI", story)
        self.assertNotIn("glossy", story)
        themed = ad_creatives.compose_image_prompt("story", records[3]["post"]["visual"], "solar")
        self.assertIn("sunflower yellow, black and cobalt blue dominate", themed)
        self.assertIn("relay team of five swimmers", story)
        self.assertIn("diving into a night pool", story)
        self.assertIn("Splashes frozen in mid-air", story)
        self.assertIn("Camera: low vertical tracking shot", story)
        self.assertIn("Colour palette: electric purple, silver, black, bold and saturated", story)
        self.assertIn("No text, letters", story)
        self.assertNotIn("Small parts working together", story)  # the abstract concept is not drawn
        self.assertGreaterEqual(len(story), 500)  # the image stage requires 500-2400 chars
        self.assertLessEqual(len(story), 2400)
        explainer = records[1]["background_prompt"]
        self.assertTrue(explainer.startswith("Scroll-stopping social media ad image") and "Isometric 3D miniature diorama" in explainer)

    def test_full_ad_prompt_carries_layout_exact_text_and_picture(self):
        with patch.object(config, "AD_TEXT_MODE", "ai"):
            records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))
        story = records[3]
        prompt = story["image_prompt"]
        self.assertEqual(story["render_mode"], "ai")
        self.assertTrue(prompt.startswith("A finished, professionally designed"))
        self.assertIn("9:16 tall vertical format", prompt)
        self.assertIn('the headline "Five small models matched a giant"', prompt)
        self.assertIn('the button "Get Started Free"', prompt)
        self.assertIn("Do not draw any logo or brand name", prompt)
        self.assertIn("#0A1F3C", prompt)
        self.assertNotIn("wordmark", prompt)
        self.assertIn("LAYOUT: Full-bleed vertical story", prompt)
        self.assertIn("relay team of five swimmers", prompt)
        # The text block comes before the picture details, and the prompt fits Cloudflare's limit.
        self.assertLess(prompt.index("TEXT:"), prompt.index("PICTURE:"))
        self.assertLessEqual(len(prompt), ad_creatives.MAX_PROMPT_CHARS)
        self.assertEqual(story["ad_text"], {"headline": "Five small models matched a giant",
                                            "cta": "Get Started Free"})
        self.assertEqual(story["logo_corner"], "top-center")

    def test_overlay_mode_keeps_the_text_free_picture_prompt(self):
        with patch.object(config, "AD_TEXT_MODE", "overlay"):
            records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))
        self.assertEqual(records[0]["image_prompt"], records[0]["background_prompt"])
        self.assertIn("No text, letters", records[0]["image_prompt"])

    def test_bad_brief_is_sent_back_once_with_the_errors(self):
        bad = _good_brief()
        bad["creatives"][0]["headline"] = "This is a very long headline that goes on and on"
        bad["creatives"][1]["visual"]["focal_subject"] = "A glowing brain made of neural network nodes floating in space."
        bad["creatives"][4]["cta"] = "Buy Now"
        client = FakeGemini([json.dumps(bad), json.dumps(_good_brief())])
        records = ad_creatives.generate_trend_creatives(_trend(), client)
        self.assertEqual(len(client.prompts), 2)
        repair = client.prompts[1]
        self.assertIn("headline must be 1-8 words", repair)
        self.assertIn("glowing brain", repair)
        self.assertIn("cta must be one of", repair)
        self.assertTrue(all(r["status"] == "success" for r in records))

    def test_still_invalid_after_repair_is_reported_not_hidden(self):
        bad = _good_brief()
        bad["creatives"][2]["hashtags"] = []
        client = FakeGemini([json.dumps(bad)] * 3)
        records = ad_creatives.generate_trend_creatives(_trend(), client)
        self.assertEqual(records[2]["status"], "failed")
        self.assertIn("hashtags", records[2]["error_message"])
        self.assertIsNone(records[2]["image_prompt"])
        self.assertEqual(sum(r["status"] == "success" for r in records), 4)

    def test_conversion_must_show_people_and_workbenches_are_banned(self):
        brief = _good_brief()
        brief["creatives"][4]["visual"]["focal_subject"] = "Reading glasses resting on a printed floor plan spread across a table."
        brief["creatives"][4]["visual"]["action"] = "catching the light"
        brief["creatives"][1]["visual"]["focal_subject"] = "Five small gears on a scarred oak workbench turning one large gear."
        errors = ad_creatives.validate_brief(brief)
        self.assertTrue(any("must show people" in e for e in errors))
        self.assertTrue(any("workbench" in e for e in errors))

    def test_each_format_tells_gemini_its_visual_medium(self):
        request = ad_creatives.make_brief_request(_trend())
        self.assertIn("VISUAL: ISOMETRIC MINIATURE WORLD", request)
        self.assertIn("VISUAL: CANDID PHOTO OF PEOPLE", request)

    def test_json_mode_bare_list_is_accepted(self):
        bare = json.dumps(_good_brief()["creatives"])  # what Gemini's JSON mode returned live
        records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([bare]))
        self.assertTrue(all(r["status"] == "success" for r in records))

    def test_good_ads_from_an_earlier_attempt_are_kept(self):
        """Attempt 1 has one bad ad; attempt 2 is unreadable; the four good ads survive."""
        first = _good_brief()
        first["creatives"][3]["caption"] = "too short"
        client = FakeGemini([json.dumps(first), "not json at all", json.dumps(first)])
        records = ad_creatives.generate_trend_creatives(_trend(), client)
        self.assertEqual([r["status"] for r in records],
                         ["success", "success", "success", "failed", "success"])

    def test_duplicate_subjects_are_rejected(self):
        brief = _good_brief()
        brief["creatives"][1]["visual"]["focal_subject"] = brief["creatives"][0]["visual"]["focal_subject"]
        self.assertIn("each ad needs a different visual.focal_subject", ad_creatives.validate_brief(brief))

    def test_stock_props_and_shared_colours_are_rejected(self):
        brief = _good_brief()
        brief["creatives"][0]["visual"]["focal_subject"] = "A heavy brass padlock fastened across a turbine intake valve."
        brief["creatives"][2]["visual"]["colors"] = brief["creatives"][3]["visual"]["colors"]
        errors = ad_creatives.validate_brief(brief)
        self.assertTrue(any("padlock" in e for e in errors), errors)
        self.assertTrue(any("own visual.colors" in e for e in errors), errors)

    def test_copied_example_subjects_are_rejected(self):
        brief = _good_brief()
        brief["creatives"][2]["visual"]["focal_subject"] = "A tiny confident hummingbird and a huge sleepy whale at a start line."
        errors = ad_creatives.validate_brief(brief)
        self.assertTrue(any("copies the example 'hummingbird'" in e for e in errors), errors)

    def test_list_colours_are_joined_into_the_prompt(self):
        brief = _good_brief()
        brief["creatives"][0]["visual"]["colors"] = ["dusty gold", "rust red", "charcoal"]
        records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(brief)]))
        self.assertEqual(records[0]["status"], "success", records[0]["error_message"])
        self.assertIn("Colour palette: dusty gold, rust red, charcoal, bold", records[0]["image_prompt"])
        self.assertNotIn("[", records[0]["image_prompt"])

    def test_every_visual_field_is_required(self):
        brief = _good_brief()
        del brief["creatives"][0]["visual"]["camera"]
        brief["creatives"][0]["visual"]["scroll_stopper"] = ""
        errors = ad_creatives.validate_brief(brief)
        self.assertIn("hook: visual.camera is missing", errors)
        self.assertIn("hook: visual.scroll_stopper is missing", errors)

    def test_image_stage_uses_each_ads_own_ratio_and_type_in_filename(self):
        records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))

        class Client:
            def text_to_image(self, **kwargs):
                self.request = kwargs

                class Img:
                    def save(self, path, format):
                        open(path, "wb").write(b"x")
                return Img()

        client = Client()
        with tempfile.TemporaryDirectory() as temp_dir:
            result = generate_images.generate_image_record(records[4], client, temp_dir)
            self.assertEqual(result["status"], "success", result["error_message"])
            self.assertEqual(result["aspect_ratio"], "1.91:1")
            self.assertEqual((client.request["width"], client.request["height"]), config.AD_ASPECT_SIZES["1.91:1"])
            self.assertTrue(result["image_path"].endswith("_conversion_1_91x1.png"))

    def test_finished_ad_is_composed_for_every_ratio(self):
        from PIL import Image

        from image_generator import compose_ads

        with tempfile.TemporaryDirectory() as temp_dir:
            for ratio, (w, h) in config.AD_ASPECT_SIZES.items():
                bg = os.path.join(temp_dir, "bg.png")
                Image.new("RGB", (w, h), (200, 180, 160)).save(bg)
                out = compose_ads.compose_ad(bg, os.path.join(temp_dir, ratio.replace(":", "x") + ".png"),
                                             ratio, "Five small models matched a giant",
                                             "Coordination, not size, closed the gap.", "Get Started Free",
                                             eyebrow="Open model", points=["Five models", "One GPU"])
                with Image.open(out) as composed:
                    self.assertEqual(composed.size, (w, h), ratio)
                    tx0, ty0, _, ty1 = compose_ads.LAYOUTS[ratio]["text"]
                    # The text panel is brand navy, not the plain picture colour.
                    self.assertNotEqual(composed.getpixel((int(w * tx0) // 2, int(h * (ty0 + ty1) / 2))),
                                        (200, 180, 160), ratio)

    def test_image_stage_writes_the_finished_ad(self):
        records = ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))
        from PIL import Image

        class Client:
            def text_to_image(self, **kwargs):
                return Image.new("RGB", (kwargs["width"], kwargs["height"]), (90, 90, 120))

        with tempfile.TemporaryDirectory() as temp_dir:
            overlay = dict(records[0], render_mode="overlay", image_prompt=records[0]["background_prompt"])
            result = generate_images.generate_image_record(overlay, Client(), temp_dir)
            self.assertTrue(result["ad_path"].endswith("_hook_4x5_ad.png"))
            self.assertTrue(os.path.isfile(result["ad_path"]))
            # AI mode without a text checker: the drawn ad plus the real logo is the finished ad.
            ai = dict(records[0], render_mode="ai", logo_corner="top-left")
            result = generate_images.generate_image_record(ai, Client(), temp_dir)
            self.assertTrue(result["ad_path"].endswith("_hook_4x5_ad.png"))
            self.assertTrue(result["image_path"].endswith("_hook_4x5.png"))
            # The logo landed in the top-left corner and nowhere else.
            from PIL import ImageChops

            ad = Image.open(result["ad_path"]).convert("RGB")
            changed = ImageChops.difference(ad, Image.new("RGB", ad.size, (90, 90, 120))).getbbox()
            self.assertIsNotNone(changed)
            self.assertLess(changed[2], ad.width // 2)
            self.assertLess(changed[3], ad.height // 3)
            self.assertEqual(result["text_check"], "unverified (no text checker)")

    def _ai_record(self):
        with patch.object(config, "AD_TEXT_MODE", "ai"):
            return ad_creatives.generate_trend_creatives(_trend(), FakeGemini([json.dumps(_good_brief())]))[0]

    def _counting_client(self):
        from PIL import Image

        class Client:
            prompts = []

            def text_to_image(self, **kwargs):
                self.prompts.append(kwargs["prompt"])
                return Image.new("RGB", (kwargs["width"], kwargs["height"]), (90, 90, 120))
        return Client()

    def test_misspelled_ad_is_redrawn_until_the_text_is_right(self):
        record, client = self._ai_record(), self._counting_client()
        transcripts = iter(["R2C.Ai\nFive smal modles matchd a gaint\nGet Startd Free",
                            "R2C.Ai\nFive small models matched a giant\nGet Started Free"])
        with tempfile.TemporaryDirectory() as temp_dir:
            result = generate_images.generate_image_record(record, client, temp_dir, lambda _img: next(transcripts))
        self.assertEqual(result["text_check"], "passed")
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(len(result["text_checks"]), 2)
        self.assertTrue(result["text_checks"][0]["problems"])
        self.assertTrue(result["ad_path"].endswith("_hook_4x5_ad.png"))

    def test_ad_that_stays_misspelled_falls_back_to_the_text_overlay(self):
        record, client = self._ai_record(), self._counting_client()
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(config, "AD_TEXT_RETRIES", 2):
            result = generate_images.generate_image_record(record, client, temp_dir, lambda _img: "R2G.Al Fiev smal")
            self.assertEqual(result["text_check"], "failed, used text overlay")
            self.assertEqual(len(client.prompts), 4)  # 3 full-ad tries + 1 text-free picture
            self.assertEqual(client.prompts[-1], record["background_prompt"])
            self.assertTrue(result["ad_path"].endswith("_hook_4x5_ad.png"))
            self.assertTrue(os.path.isfile(result["ad_path"]))

    def test_text_check_rules(self):
        from image_generator.text_check import check_ad_text

        want = {"headline": "Five small models matched a giant", "cta": "Get Started Free", "brand": "R2C.Ai"}
        ok, problems = check_ad_text("R2C.Ai\nFIVE SMALL MODELS MATCHED A GIANT\nGet Started Free →", want)
        self.assertTrue(ok, problems)
        self.assertFalse(check_ad_text("R2C.Ai Five small modles matched a giant Get Started Free", want)[0])
        self.assertIn("R2C.Ai wordmark missing or misspelled",
                      check_ad_text("R2G.Al Five small models matched a giant Get Started Free", want)[1])
        self.assertTrue(any("extra text" in p for p in check_ad_text(
            "R2C.Ai Five small models matched a giant Get Started Free lorem ipsum dolor sit amet", want)[1]))
        self.assertEqual(check_ad_text("NONE", want), (False, ["no text found in the image"]))
        # Logo placed by code: the drawn text need not contain the brand.
        no_brand = {"headline": "Five small models matched a giant", "cta": "Get Started Free"}
        self.assertTrue(check_ad_text("Five small models matched a giant" + chr(10) + "Get Started Free", no_brand)[0])

    def test_near_misses_are_loaded_as_extra_trends(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "dropped.json")
            json.dump([{"cluster_id": "cls_009", "topic_title": "Banks detect AI agents", "near_miss": True,
                        "source_urls": ["https://a.example"]},
                       {"cluster_id": "cls_010", "topic_title": "Off topic", "near_miss": False}],
                      open(path, "w", encoding="utf-8"))
            trends = ad_creatives.load_near_misses(path, start_rank=8)
        self.assertEqual([t["trend_id"] for t in trends], ["near_cls_009"])
        self.assertEqual(trends[0]["rank"], 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
