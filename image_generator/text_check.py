"""
text_check.py — Check that an AI-designed ad spells its text correctly.

Gemini transcribes the text it sees in the generated ad; this module compares
the transcript with the text the ad was supposed to carry (headline, CTA button,
R2C.Ai wordmark) and reports anything missing, misspelled or added.
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Tuple

MAX_EXTRA_WORDS = 0  # any stray word ("Peurice.") is a typo in a published ad

# The real logo is stamped by code and Gemini may still read it.
IGNORED_WORDS = {"r2c", "ai"}


def normalise(text: str) -> str:
    text = (text or "").lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9.' ]+", " ", text)
    return " ".join(text.replace(".", " ").replace("'", "").split())


def _best_match(expected: str, transcript: str) -> float:
    """Best similarity of `expected` against any same-length run of words in the transcript."""
    want, words = normalise(expected), normalise(transcript).split()
    if not want:
        return 1.0
    size = len(want.split())
    best = 0.0
    for length in (size - 1, size, size + 1):
        if length < 1:
            continue
        for start in range(0, max(1, len(words) - length + 1)):
            window = " ".join(words[start:start + length])
            best = max(best, difflib.SequenceMatcher(None, want, window).ratio())
    return best


def _contains_exactly(expected: str, transcript: str) -> bool:
    """Every word of `expected`, spelled exactly, in order and next to each other."""
    want, words = normalise(expected).split(), normalise(transcript).split()
    if not want:
        return True
    return any(words[i:i + len(want)] == want for i in range(len(words) - len(want) + 1))


def check_ad_text(transcript: str, expected: Dict[str, str]) -> Tuple[bool, List[str]]:
    """
    (ok, problems) for a transcript against {"headline", "cta", "brand"}.

    Spelling must be exact: one swapped letter ("modles") is a typo in a
    published ad, so near-matches are reported with their similarity but fail.
    Case, punctuation and line breaks are ignored.
    """
    problems: List[str] = []
    if not transcript or transcript.strip().upper() == "NONE":
        return False, ["no text found in the image"]
    for name, label in (("headline", "headline"), ("cta", "CTA button")):
        if not _contains_exactly(expected.get(name, ""), transcript):
            problems.append("{} misspelled or missing (closest match {:.0%})".format(
                label, _best_match(expected.get(name, ""), transcript)))
    flat = normalise(transcript)
    if expected.get("brand") and "r2c" not in flat.replace(" ", ""):
        problems.append("R2C.Ai wordmark missing or misspelled")
    expected_words = set(normalise(" ".join(expected.values())).split())
    extra = [w for w in flat.split() if w not in expected_words and len(w) > 1 and w not in IGNORED_WORDS]
    if len(extra) > MAX_EXTRA_WORDS:
        problems.append("extra text in the image: {}".format(" ".join(extra[:8])))
    return not problems, problems
