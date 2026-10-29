"""Automated tests for the R2C automation workflow."""
import os

# Tests must never use a developer's local API keys.
os.environ["GEMINI_API_KEY"] = ""
