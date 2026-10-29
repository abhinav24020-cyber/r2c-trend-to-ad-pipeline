"""Tests for loading local environment variables without exposing secrets."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

# Keep importing project config in tests from enabling live model requests.
os.environ["GEMINI_API_KEY"] = ""

from trend_pipeline.config import load_env_file


class TestEnvFileLoading(unittest.TestCase):
    def test_reads_values_and_preserves_existing_environment(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", suffix=".env", delete=False
        ) as env_file:
            env_file.write(
                "# comment\n"
                "TEST_CONFIG_FROM_FILE=file-value\n"
                'TEST_CONFIG_QUOTED="quoted value"\n'
                "TEST_CONFIG_FROM_PROCESS=file-value\n"
            )
            path = env_file.name
        try:
            with patch.dict(
                os.environ,
                {"TEST_CONFIG_FROM_PROCESS": "process-value"},
                clear=True,
            ):
                load_env_file(path)
                self.assertEqual(os.environ["TEST_CONFIG_FROM_FILE"], "file-value")
                self.assertEqual(os.environ["TEST_CONFIG_QUOTED"], "quoted value")
                self.assertEqual(
                    os.environ["TEST_CONFIG_FROM_PROCESS"], "process-value"
                )
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
