from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from trend_pipeline.gemini_client import GeminiClient


class _FakeEmbeddingModel:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    def embed_content(self, model: str, contents: list[str]) -> SimpleNamespace:
        self.batch_sizes.append(len(contents))
        return SimpleNamespace(
            embeddings=[
                SimpleNamespace(
                    values=[float(text) if text.isdigit() else float(ord(text[0])), 1.0]
                )
                for text in contents
            ]
        )


class TestGeminiEmbeddings(unittest.TestCase):
    def test_large_input_is_embedded_in_ordered_api_sized_batches(self) -> None:
        model = _FakeEmbeddingModel()
        client = GeminiClient.__new__(GeminiClient)
        client.online = True
        client._client = SimpleNamespace(models=model)
        client.calls_attempted = 0
        client.calls_failed = 0

        with patch("trend_pipeline.gemini_client.time.sleep") as sleep:
            vectors = client.embed_texts([str(index) for index in range(205)])

        self.assertEqual(model.batch_sizes, [100, 100, 5])
        self.assertEqual(len(vectors), 205)
        self.assertEqual([vector[0] for vector in vectors], [float(i) for i in range(205)])
        self.assertEqual(client.calls_attempted, 3)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(60.0), unittest.mock.call(60.0)])

    def test_large_text_batches_respect_character_budget(self) -> None:
        model = _FakeEmbeddingModel()
        client = GeminiClient.__new__(GeminiClient)
        client.online = True
        client._client = SimpleNamespace(models=model)
        client.calls_attempted = 0
        client.calls_failed = 0

        with patch("trend_pipeline.gemini_client.time.sleep") as sleep:
            vectors = client.embed_texts(["a" * 40_000, "b" * 40_000, "c" * 40_000])

        self.assertEqual(model.batch_sizes, [2, 1])
        self.assertEqual(len(vectors), 3)
        self.assertEqual([vector[0] for vector in vectors], [97.0, 98.0, 99.0])
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(60.0)])


if __name__ == "__main__":
    unittest.main()
