"""
gemini_client.py — Thin, retrying wrapper around the `google-genai` SDK.

Every Gemini call in Gate 2 goes through here so that retry/backoff, credential
handling and the offline fallback are implemented exactly once.

Offline fallback: when GEMINI_API_KEY is unset or the SDK is unavailable and
config.ALLOW_OFFLINE_FALLBACK is True, embeddings fall back to local hashed
TF-IDF vectors and relevance scores fall back to keyword heuristics. The demo
therefore always runs; GeminiClient.online records which mode produced a result.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Dict, List, Optional, Sequence

from . import config, text_utils

logger = logging.getLogger("gemini")

_HASHED_DIM = 256
_EMBEDDING_BATCH_SIZE = 100
_EMBEDDING_BATCH_CHAR_LIMIT = 80_000


class GeminiUnavailableError(RuntimeError):
    """Raised when Gemini cannot be reached and offline fallback is disabled."""


def hash_token(token: str) -> int:
    return int.from_bytes(hashlib.md5(token.encode("utf-8")).digest()[:4], "big")


_RETRY_IN_RE = re.compile(r"retry in ([\d.]+)\s*s", re.IGNORECASE)


def quota_retry_seconds(exc: Exception) -> Optional[float]:
    """
    For a 429 / RESOURCE_EXHAUSTED error, how long to wait before retrying.

    The free tier is a per-minute window, so the default 1-2-4 s backoff burns
    all retries inside the same window. Use the server's "retry in N s" hint,
    else wait out a full minute. Returns None for any other error.
    """
    text = str(exc)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
        return None
    match = _RETRY_IN_RE.search(text)
    wait = float(match.group(1)) + 1.0 if match else 60.0
    return max(5.0, min(wait, 65.0))


class GeminiClient:
    """Lazily-initialised Gemini client with exponential-backoff retries."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key: str = api_key if api_key is not None else config.GEMINI_API_KEY
        self._client = None
        self.online: bool = False
        self.calls_attempted: int = 0
        self.calls_failed: int = 0
        # Which backend produced the most recent embed_texts() result. Clustering
        # thresholds depend on it: a mid-run quota error falls back to lexical
        # vectors, which live on a different cosine scale than Gemini's.
        self.embedding_mode: Optional[str] = None
        self._last_generate_at: float = 0.0
        self._init_client()

    # -- setup -------------------------------------------------------------
    def _init_client(self) -> None:
        if not self.api_key:
            logger.warning(
                "GEMINI_API_KEY not set - Gate 2 will use local heuristic fallbacks."
            )
            self._require_fallback_allowed("missing GEMINI_API_KEY")
            return
        try:
            from google import genai  # type: ignore

            self._client = genai.Client(api_key=self.api_key)
            self.online = True
            logger.info(
                "Gemini client initialised (embed=%s, text=%s)",
                config.GEMINI_EMBEDDING_MODEL,
                config.GEMINI_TEXT_MODEL,
            )
        except Exception as exc:  # SDK missing or client construction failed
            logger.warning(
                "Could not initialise google-genai SDK (%s) - using fallbacks.", exc
            )
            self._require_fallback_allowed("SDK init failed: {}".format(exc))

    @staticmethod
    def _require_fallback_allowed(reason: str) -> None:
        if not config.ALLOW_OFFLINE_FALLBACK:
            raise GeminiUnavailableError(
                "Gemini unavailable ({}) and ALLOW_OFFLINE_FALLBACK is False.".format(reason)
            )

    # -- retry core --------------------------------------------------------
    def _with_backoff(self, label: str, fn):
        """Run fn with exponential backoff; return None once retries are exhausted."""
        delay = config.API_INITIAL_BACKOFF_SECONDS
        last_exc: Optional[Exception] = None
        for attempt in range(1, config.API_MAX_RETRIES + 1):
            self.calls_attempted += 1
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001 - SDK raises a wide range
                last_exc = exc
                self.calls_failed += 1
                if attempt == config.API_MAX_RETRIES:
                    break
                wait = quota_retry_seconds(exc)
                if wait is None:
                    wait = delay
                    delay = min(
                        delay * config.API_BACKOFF_MULTIPLIER, config.API_MAX_BACKOFF_SECONDS
                    )
                logger.warning(
                    "%s failed (attempt %d/%d): %s - retrying in %.1fs",
                    label, attempt, config.API_MAX_RETRIES, str(exc)[:160], wait,
                )
                time.sleep(wait)
        logger.error("%s exhausted %d retries: %s", label, config.API_MAX_RETRIES, last_exc)
        self._require_fallback_allowed("{} failed: {}".format(label, last_exc))
        return None

    # -- embeddings --------------------------------------------------------
    def embed_texts(self, texts: Sequence[str]) -> List[List[float]]:
        """Return one dense vector per input text (Gemini, else local fallback)."""
        if not texts:
            return []
        if self.online and self._client is not None:
            vectors: List[List[float]] = []
            batches: List[List[str]] = []
            batch: List[str] = []
            batch_chars = 0
            for text in texts:
                if batch and (
                    len(batch) >= _EMBEDDING_BATCH_SIZE
                    or batch_chars + len(text) > _EMBEDDING_BATCH_CHAR_LIMIT
                ):
                    batches.append(batch)
                    batch = []
                    batch_chars = 0
                batch.append(text)
                batch_chars += len(text)
            if batch:
                batches.append(batch)

            for batch_index, batch in enumerate(batches):
                if batch_index:
                    logger.info(
                        "Waiting 60 seconds before embedding batch %d/%d to respect "
                        "the free-tier per-minute quota.",
                        batch_index + 1, len(batches),
                    )
                    time.sleep(60.0)
                result = self._with_backoff(
                    "embed_content batch {}".format(batch_index + 1),
                    lambda batch=batch: self._client.models.embed_content(
                        model=config.GEMINI_EMBEDDING_MODEL, contents=list(batch)
                    ),
                )
                batch_vectors = self._unpack_embeddings(result)
                if batch_vectors is None or len(batch_vectors) != len(batch):
                    logger.warning(
                        "Falling back to local hashed embeddings after batch %d failed.",
                        batch_index + 1,
                    )
                    self.embedding_mode = "fallback"
                    return [self._fallback_embedding(text) for text in texts]
                vectors.extend(batch_vectors)

            if len(vectors) == len(texts):
                logger.info(
                    "Embedded %d texts via %s (dim=%d)",
                    len(texts), config.GEMINI_EMBEDDING_MODEL, len(vectors[0]),
                )
                self.embedding_mode = "gemini"
                return vectors
        self.embedding_mode = "fallback"
        return [self._fallback_embedding(t) for t in texts]

    @staticmethod
    def _unpack_embeddings(result) -> Optional[List[List[float]]]:
        if result is None:
            return None
        try:
            embeddings = getattr(result, "embeddings", None)
            if embeddings is None:
                return None
            out: List[List[float]] = []
            for item in embeddings:
                values = getattr(item, "values", None)
                out.append([float(v) for v in (values if values is not None else item)])
            return out or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Unexpected embedding payload shape: %s", exc)
            return None

    @staticmethod
    def _fallback_embedding(text: str) -> List[float]:
        """Deterministic hashed bag-of-words vector - a usable stand-in for clustering."""
        vec = [0.0] * _HASHED_DIM
        local_idf = {tok: 1.0 for tok in text_utils.tokenize(text)}
        for token, weight in text_utils.tfidf_vector(text, local_idf).items():
            vec[hash_token(token) % _HASHED_DIM] += weight
        return vec

    # -- generation --------------------------------------------------------
    def generate_text(
        self, prompt: str, label: str = "generate_content", json_mode: bool = False
    ) -> Optional[str]:
        """json_mode asks Gemini to return valid JSON (response_mime_type=application/json)."""
        if not (self.online and self._client is not None):
            return None
        kwargs = {"config": {"response_mime_type": "application/json"}} if json_mode else {}

        def call():
            self._pace()
            return self._client.models.generate_content(
                model=config.GEMINI_TEXT_MODEL, contents=prompt, **kwargs
            )

        result = self._with_backoff(label, call)
        if result is None:
            return None
        text = getattr(result, "text", None)
        return text.strip() if isinstance(text, str) and text.strip() else None

    def read_image_text(self, image: object, label: str = "read_image_text") -> Optional[str]:
        """Transcribe all text visible in an image (PIL image). None when Gemini is unavailable."""
        if not (self.online and self._client is not None):
            return None
        prompt = ("You are proofreading an advert. Transcribe every piece of text visible in this image "
                  "exactly as it is drawn, letter by letter, one item per line. Do NOT correct anything: "
                  "if a letter is malformed, merged, doubled, missing or looks like a different letter, "
                  "write what it actually looks like (for example 'Uploαd' or 'Uplaod'), never the word "
                  "that was probably intended. Ignore logos. If there is no text at all, reply NONE.")

        def call():
            self._pace()
            return self._client.models.generate_content(
                model=config.GEMINI_TEXT_MODEL, contents=[image, prompt]
            )

        result = self._with_backoff(label, call)
        text = getattr(result, "text", None) if result is not None else None
        return text.strip() if isinstance(text, str) else None

    def _pace(self) -> None:
        """Space text-generation calls to stay under GEMINI_MAX_REQUESTS_PER_MINUTE."""
        rate = config.GEMINI_MAX_REQUESTS_PER_MINUTE
        if rate <= 0:
            return
        wait = self._last_generate_at + 60.0 / rate - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_generate_at = time.monotonic()

    def score_0_100(self, prompt: str, label: str = "score") -> Optional[float]:
        """Ask Gemini for a bare 0-100 number; parse defensively."""
        raw = self.generate_text(prompt, label=label)
        if not raw:
            return None
        match = re.search(r"\d{1,3}(?:\.\d+)?", raw)
        if not match:
            logger.warning("%s: no number in model reply: %s", label, raw[:80])
            return None
        return max(0.0, min(100.0, float(match.group())))

    def stats(self) -> Dict[str, object]:
        return {
            "mode": "gemini" if self.online else "offline_fallback",
            "calls_attempted": self.calls_attempted,
            "calls_failed": self.calls_failed,
        }


_SHARED: Optional[GeminiClient] = None


def get_client() -> GeminiClient:
    """Process-wide singleton so one client (and one retry budget) is reused."""
    global _SHARED
    if _SHARED is None:
        _SHARED = GeminiClient()
    return _SHARED
