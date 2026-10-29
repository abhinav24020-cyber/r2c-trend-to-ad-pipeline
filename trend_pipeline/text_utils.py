"""
text_utils.py — Dependency-free text similarity helpers shared by Gate 1 and Gate 2.

Kept local (no scikit-learn / no GPU) so the structural filtering in Gate 1 runs
deterministically and offline; only semantic work in Gate 2 calls out to Gemini.
"""
from __future__ import annotations

import hashlib
import html
import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Sequence, Set, Tuple

from . import config

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-\+\.']*")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HASHTAG_RE = re.compile(r"#\w+")
_MENTION_RE = re.compile(r"@\w+")

STOPWORDS: Set[str] = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "her",
    "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "its",
    "new", "now", "old", "see", "two", "way", "who", "boy", "did", "man", "men",
    "too", "use", "with", "this", "that", "from", "have", "they", "will", "been",
    "were", "what", "when", "your", "just", "like", "some", "more", "than",
    "then", "them", "about", "would", "there", "their", "which", "into", "also",
    "only", "very", "over", "after", "could", "should", "other", "these", "most",
    "even", "much", "well", "make", "made", "back", "good", "here", "does",
    "think", "really", "still", "going", "thing", "things", "https", "http",
    "www", "com", "amp", "via", "one", "post", "posted", "says", "said",
}


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Reduce an HTML fragment (e.g. a Mastodon status) to whitespace-normalised text."""
    return " ".join(html.unescape(_TAG_RE.sub(" ", text or "")).split())


def strip_noise(text: str) -> str:
    """Remove URLs / mentions so similarity is driven by substance, not boilerplate."""
    text = _URL_RE.sub(" ", text or "")
    text = _MENTION_RE.sub(" ", text)
    return text


def tokenize(text: str, drop_stopwords: bool = True, min_len: int = 3) -> List[str]:
    tokens = _TOKEN_RE.findall(strip_noise(text or "").lower())
    out = [t for t in tokens if len(t) >= min_len]
    if drop_stopwords:
        out = [t for t in out if t not in STOPWORDS]
    return out


def extract_keywords(text: str, top_k: int = 12) -> List[str]:
    """Most frequent non-stopword tokens — used for Gate 1 topic grouping."""
    counts = Counter(tokenize(text))
    return [tok for tok, _ in counts.most_common(top_k)]


def normalize_for_hash(text: str) -> str:
    """Canonical form for exact-duplicate hashing (case/space/punct insensitive)."""
    return " ".join(tokenize(strip_noise(text), drop_stopwords=False, min_len=1))


def content_hash(text: str) -> str:
    return hashlib.md5(normalize_for_hash(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# MinHash / LSH-style near-duplicate estimation
# ---------------------------------------------------------------------------
_MASK64 = (1 << 64) - 1


def _shingles(tokens: Sequence[str], size: int) -> Set[str]:
    if len(tokens) < size:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1)}


def _stable_hash(value: str, seed: int) -> int:
    digest = hashlib.md5(f"{seed}:{value}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & _MASK64


def minhash_signature(
    text: str,
    permutations: int = config.MINHASH_PERMUTATIONS,
    shingle_size: int = config.MINHASH_SHINGLE_SIZE,
) -> Tuple[int, ...]:
    tokens = tokenize(text, drop_stopwords=False)
    shingles = _shingles(tokens, shingle_size)
    if not shingles:
        return tuple([_MASK64] * permutations)
    return tuple(
        min(_stable_hash(sh, seed) for sh in shingles) for seed in range(permutations)
    )


def minhash_similarity(a: Tuple[int, ...], b: Tuple[int, ...]) -> float:
    """Estimated Jaccard similarity between two MinHash signatures."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


# ---------------------------------------------------------------------------
# TF-IDF vectors + cosine similarity
# ---------------------------------------------------------------------------
def build_idf(documents: Iterable[str]) -> Dict[str, float]:
    docs = list(documents)
    n = max(len(docs), 1)
    df: Counter = Counter()
    for doc in docs:
        for token in set(tokenize(doc)):
            df[token] += 1
    return {tok: math.log((n + 1) / (count + 1)) + 1.0 for tok, count in df.items()}


def tfidf_vector(text: str, idf: Dict[str, float]) -> Dict[str, float]:
    counts = Counter(tokenize(text))
    if not counts:
        return {}
    max_tf = max(counts.values())
    vec = {
        tok: (0.5 + 0.5 * (tf / max_tf)) * idf.get(tok, 1.0)
        for tok, tf in counts.items()
    }
    norm = math.sqrt(sum(v * v for v in vec.values()))
    if norm == 0.0:
        return {}
    return {tok: v / norm for tok, v in vec.items()}


def cosine_sparse(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return max(0.0, min(1.0, sum(val * b.get(tok, 0.0) for tok, val in a.items())))


def cosine_dense(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


# ---------------------------------------------------------------------------
# Spam-surface measurements (consumed by gate1_quality)
# ---------------------------------------------------------------------------
def count_urls(text: str) -> int:
    return len(_URL_RE.findall(text or ""))


def count_hashtags(text: str) -> int:
    return len(_HASHTAG_RE.findall(text or ""))


def caps_ratio(text: str) -> float:
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 20:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def repeated_token_ratio(text: str) -> float:
    """Share of the text taken by its single most repeated token (keyword stuffing)."""
    tokens = tokenize(text)
    if len(tokens) < 8:
        return 0.0
    return Counter(tokens).most_common(1)[0][1] / len(tokens)
