"""
gate1_quality.py — GATE 1 (part A): per-signal data quality checks.

Each signal runs the checks below in sequence. The first failure stops the chain,
tags the signal with its rejection status, and discards it:

  1. Attribution verification ..... REJECT_UNATTRIBUTED
  2. Spam / bot filtering .......... REJECT_SPAM
  3. Deduplication (exact) ......... REJECT_DUPLICATE
  4. Near-duplicate (MinHash+TFIDF)  REJECT_DUPLICATE
  5. Credibility scoring ........... REJECT_LOW_CREDIBILITY
  6. Freshness / time-decay ........ REJECT_STALE
  7. Structured enrichment ......... (keywords, hashes, metrics attached)

Step 7 never rejects; it attaches the structured fields Gate 1's cross-platform
rule and Gate 2's clustering depend on. Cross-platform corroboration is the
eighth check and lives in gate1_cross.py, because it is a group-level rule.

Attribution is checked first (cheapest, and a signal with no source cannot be
credited or corroborated anyway).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import config, text_utils
from .ingest import RawSignal

logger = logging.getLogger("gate1.quality")


# Rejection statuses
REJECT_SPAM = "REJECT_SPAM"
REJECT_DUPLICATE = "REJECT_DUPLICATE"
REJECT_ALREADY_PROCESSED = "REJECT_ALREADY_PROCESSED"
REJECT_LOW_CREDIBILITY = "REJECT_LOW_CREDIBILITY"
REJECT_STALE = "REJECT_STALE"
REJECT_UNATTRIBUTED = "REJECT_UNATTRIBUTED"
REJECT_SINGLE_SOURCE = "REJECT_SINGLE_SOURCE"  # applied in gate1_cross
PASSED = "PASSED_QUALITY_CHECKS"

ALL_REJECTION_STATUSES: List[str] = [
    REJECT_SPAM,
    REJECT_DUPLICATE,
    REJECT_ALREADY_PROCESSED,
    REJECT_LOW_CREDIBILITY,
    REJECT_STALE,
    REJECT_UNATTRIBUTED,
    REJECT_SINGLE_SOURCE,
]


@dataclass
class QualityVerdict:
    """Outcome of Gate 1 part A for a single signal."""

    signal: RawSignal
    status: str = PASSED
    reason: str = ""
    is_spam: bool = False
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    credibility_score: float = 0.0
    credibility_breakdown: Dict[str, float] = field(default_factory=dict)
    freshness_score: float = 0.0
    age_hours: Optional[float] = None
    content_hash: str = ""
    minhash: Tuple[int, ...] = ()
    keywords: List[str] = field(default_factory=list)
    cross_platform_confirmed: bool = False
    platforms_detected: List[str] = field(default_factory=list)
    corroboration_group: Optional[str] = None
    user_selected_source_exception: bool = False

    @property
    def passed(self) -> bool:
        return self.status == PASSED


# ---------------------------------------------------------------------------
# Step 1 — Attribution verification
# ---------------------------------------------------------------------------
def check_attribution(signal: RawSignal) -> Optional[str]:
    """source_url, author.author_id and source_platform must all be present."""
    missing: List[str] = []
    if not signal.source_url:
        missing.append("source_url")
    if not signal.author.author_id:
        missing.append("author.author_id")
    if not signal.source_platform:
        missing.append("source_platform")
    if not signal.timestamp:
        missing.append("timestamp")
    if missing:
        return "missing attribution fields: {}".format(", ".join(missing))
    return None


# ---------------------------------------------------------------------------
# Step 2 — Spam / bot filtering
# ---------------------------------------------------------------------------
def check_spam(signal: RawSignal) -> Optional[str]:
    text = signal.text
    lowered = text.lower()

    hits = [kw for kw in config.SPAM_KEYWORDS if kw in lowered]
    if hits:
        return "promo keywords: {}".format(", ".join(hits[:3]))

    urls = text_utils.count_urls(text)
    if urls > config.MAX_URLS:
        return "URL stuffing ({} links > {})".format(urls, config.MAX_URLS)

    tags = text_utils.count_hashtags(text)
    if tags > config.MAX_HASHTAGS:
        return "hashtag stuffing ({} tags > {})".format(tags, config.MAX_HASHTAGS)

    caps = text_utils.caps_ratio(text)
    if caps > config.MAX_CAPS_RATIO:
        return "all-caps shouting (caps ratio {:.2f})".format(caps)

    repetition = text_utils.repeated_token_ratio(text)
    if repetition > config.MAX_REPEATED_TOKEN_RATIO:
        return "repetitive bot syntax (token repetition {:.2f})".format(repetition)

    if len(text.strip()) < config.MIN_CONTENT_CHARS:
        return "content too thin ({} chars < {})".format(
            len(text.strip()), config.MIN_CONTENT_CHARS
        )

    # Bot-shaped account: brand-new account with an implausible follower count.
    if (
        signal.author.account_age_days < 14
        and signal.author.follower_count > 50000
    ):
        return "bot-shaped account (age {}d, {} followers)".format(
            signal.author.account_age_days, signal.author.follower_count
        )
    return None


# ---------------------------------------------------------------------------
# Steps 3 & 4 — Exact and near-duplicate detection
# ---------------------------------------------------------------------------
class DuplicateIndex:
    """Running index of accepted content, seeded with prior post history."""

    def __init__(self, idf: Dict[str, float]) -> None:
        self.idf = idf
        self._hashes: Dict[str, str] = {}
        self._entries: List[Tuple[str, Tuple[int, ...], Dict[str, float]]] = []

    def seed_history(self, history_texts: Sequence[Tuple[str, str]]) -> None:
        """history_texts: (identifier, text) pairs from earlier pipeline runs."""
        for identifier, text in history_texts:
            self.add(identifier, text)
        if history_texts:
            logger.info(
                "GATE1 | dedup index seeded with %d historical items", len(history_texts)
            )

    def add(self, identifier: str, text: str) -> None:
        self._hashes.setdefault(text_utils.content_hash(text), identifier)
        self._entries.append(
            (
                identifier,
                text_utils.minhash_signature(text),
                text_utils.tfidf_vector(text, self.idf),
            )
        )

    def find_exact(self, text: str) -> Optional[str]:
        return self._hashes.get(text_utils.content_hash(text))

    def find_near(self, text: str) -> Optional[Tuple[str, float, float]]:
        """Return (id, minhash_sim, cosine_sim) of the first near-duplicate found."""
        signature = text_utils.minhash_signature(text)
        vector = text_utils.tfidf_vector(text, self.idf)
        for identifier, other_sig, other_vec in self._entries:
            mh = text_utils.minhash_similarity(signature, other_sig)
            cos = text_utils.cosine_sparse(vector, other_vec)
            if (
                mh >= config.NEAR_DUPLICATE_MINHASH_THRESHOLD
                or cos >= config.NEAR_DUPLICATE_COSINE_THRESHOLD
            ):
                return identifier, mh, cos
        return None


# ---------------------------------------------------------------------------
# Step 5 — Credibility scoring
# ---------------------------------------------------------------------------
def score_account_age(age_days: int) -> float:
    if age_days <= 0:
        return 0.0
    if age_days < 30:
        return 15.0 * (age_days / 30.0)          # brand-new accounts stay near zero
    ratio = min(1.0, age_days / float(config.MIN_TRUSTED_ACCOUNT_AGE_DAYS))
    return 15.0 + 85.0 * ratio


def score_engagement_ratio(engagement_total: int, followers: int) -> float:
    """Reward organic traction; punish both dead accounts and inorganic spikes."""
    if followers <= 0:
        # No audience metric available (e.g. Google Search results): score on
        # absolute engagement instead of a ratio, capped conservatively.
        return min(60.0, math.log10(engagement_total + 1) * 30.0)
    ratio = engagement_total / float(followers)
    if ratio > config.SUSPICIOUS_ENGAGEMENT_RATIO:
        return 20.0                               # implausible -> likely amplified
    # 0.02 (2% engagement) is treated as a strong organic rate.
    return max(0.0, min(100.0, (ratio / 0.02) * 100.0))


def score_domain_authority(signal: RawSignal) -> float:
    domain = signal.domain
    for known, score in config.DOMAIN_AUTHORITY.items():
        if domain == known or domain.endswith("." + known):
            return score
    return config.PLATFORM_AUTHORITY.get(
        signal.source_platform, config.DEFAULT_DOMAIN_AUTHORITY
    )


def score_credibility(signal: RawSignal) -> Tuple[float, Dict[str, float]]:
    breakdown = {
        "account_age": score_account_age(signal.author.account_age_days),
        "engagement_ratio": score_engagement_ratio(
            signal.engagement_metrics.total, signal.author.follower_count
        ),
        "domain_authority": score_domain_authority(signal),
    }
    total = sum(
        breakdown[key] * weight for key, weight in config.CREDIBILITY_WEIGHTS.items()
    )
    return round(total, 2), {k: round(v, 2) for k, v in breakdown.items()}


# ---------------------------------------------------------------------------
# Step 6 — Freshness / exponential time decay
# ---------------------------------------------------------------------------
def freshness_weight(age_hours: float) -> float:
    """Weight(t) = e^(-lambda * hours_elapsed), clamped to [0, 1]."""
    return round(max(0.0, min(1.0, math.exp(-config.FRESHNESS_DECAY_LAMBDA * max(age_hours, 0.0)))), 4)


def check_freshness(signal: RawSignal) -> Tuple[Optional[str], float, Optional[float]]:
    age = signal.age_hours
    if age is None:
        return "unparseable timestamp", 0.0, None
    if age > config.FRESHNESS_CUTOFF_HOURS:
        return (
            "stale ({:.1f}h old > {:.0f}h cutoff)".format(age, config.FRESHNESS_CUTOFF_HOURS),
            freshness_weight(age),
            age,
        )
    return None, freshness_weight(age), age


# ---------------------------------------------------------------------------
# Gate 1 part A driver
# ---------------------------------------------------------------------------
def run_quality_checks(
    signals: Sequence[RawSignal],
    post_history: Optional[Sequence[Tuple[str, str]]] = None,
) -> List[QualityVerdict]:
    """Run all per-signal checks; returns one verdict per input signal, in order."""
    logger.info("GATE1 | ---- part A: per-signal quality checks (%d signals) ----", len(signals))

    # IDF is built over the whole candidate batch so near-duplicate cosine is
    # measured against this run's vocabulary rather than an arbitrary prior.
    idf = text_utils.build_idf([s.text for s in signals])
    index = DuplicateIndex(idf)
    history_ids = {signal_id for signal_id, _text in post_history or ()}
    if post_history:
        index.seed_history(post_history)

    verdicts: List[QualityVerdict] = []
    for signal in signals:
        verdict = QualityVerdict(signal=signal)
        verdict.content_hash = text_utils.content_hash(signal.text)
        verdict.keywords = signal.keywords()
        sid = signal.signal_id

        # 1. attribution
        problem = check_attribution(signal)
        if problem:
            verdict.status, verdict.reason = REJECT_UNATTRIBUTED, problem
            logger.info("GATE1 | %-24s | %-22s | %s", sid, REJECT_UNATTRIBUTED, problem)
            verdicts.append(verdict)
            continue

        # 2. spam / bot
        problem = check_spam(signal)
        if problem:
            verdict.status, verdict.reason, verdict.is_spam = REJECT_SPAM, problem, True
            logger.info("GATE1 | %-24s | %-22s | %s", sid, REJECT_SPAM, problem)
            verdicts.append(verdict)
            continue

        # 3. exact duplicate
        exact = index.find_exact(signal.text)
        if exact:
            same_signal = exact == sid
            if not (same_signal and exact in history_ids):
                verdict.status = REJECT_DUPLICATE
                verdict.is_duplicate = True
                verdict.duplicate_of = exact
                verdict.reason = "exact md5 match of {}".format(exact)
                logger.info("GATE1 | %-24s | %-22s | %s", sid, verdict.status, verdict.reason)
                verdicts.append(verdict)
                continue
            logger.debug("GATE1 | %s resurfaced from history; rechecking quality", sid)

        # 4. near duplicate
        near = index.find_near(signal.text)
        if near:
            other, mh, cos = near
            same_signal = other == sid
            if not (same_signal and other in history_ids):
                verdict.status = REJECT_DUPLICATE
                verdict.is_duplicate = True
                verdict.duplicate_of = other
                verdict.reason = "near-duplicate of {} (minhash {:.2f}, cosine {:.2f})".format(
                    other, mh, cos
                )
                logger.info("GATE1 | %-24s | %-22s | %s", sid, verdict.status, verdict.reason)
                verdicts.append(verdict)
                continue
            logger.debug("GATE1 | %s resurfaced as a near-match from history; rechecking quality", sid)

        # 5. credibility
        verdict.credibility_score, verdict.credibility_breakdown = score_credibility(signal)
        if verdict.credibility_score < config.MIN_CREDIBILITY_SCORE:
            verdict.status = REJECT_LOW_CREDIBILITY
            verdict.reason = "credibility {:.1f} < {:.1f} (age={:.0f}, engagement={:.0f}, domain={:.0f})".format(
                verdict.credibility_score,
                config.MIN_CREDIBILITY_SCORE,
                verdict.credibility_breakdown["account_age"],
                verdict.credibility_breakdown["engagement_ratio"],
                verdict.credibility_breakdown["domain_authority"],
            )
            logger.info("GATE1 | %-24s | %-22s | %s", sid, REJECT_LOW_CREDIBILITY, verdict.reason)
            verdicts.append(verdict)
            continue

        # 6. freshness
        problem, verdict.freshness_score, verdict.age_hours = check_freshness(signal)
        if problem:
            verdict.status, verdict.reason = REJECT_STALE, problem
            logger.info("GATE1 | %-24s | %-22s | %s", sid, REJECT_STALE, problem)
            verdicts.append(verdict)
            continue

        # 7. structured enrichment — index this signal so later ones dedup against it
        verdict.minhash = text_utils.minhash_signature(signal.text)
        index.add(sid, signal.text)
        logger.info(
            "GATE1 | %-24s | %-22s | credibility %.1f, freshness %.2f, %.1fh old",
            sid, PASSED, verdict.credibility_score, verdict.freshness_score,
            verdict.age_hours or 0.0,
        )
        verdicts.append(verdict)

    passed = sum(1 for v in verdicts if v.passed)
    logger.info("GATE1 | part A complete: %d passed, %d rejected", passed, len(verdicts) - passed)
    return verdicts


def rejection_summary(verdicts: Sequence[QualityVerdict]) -> Dict[str, int]:
    summary: Dict[str, int] = {status: 0 for status in ALL_REJECTION_STATUSES}
    for verdict in verdicts:
        if not verdict.passed:
            summary[verdict.status] = summary.get(verdict.status, 0) + 1
    return summary


def passing_verdicts(verdicts: Sequence[QualityVerdict]) -> List[QualityVerdict]:
    return [v for v in verdicts if v.passed]
