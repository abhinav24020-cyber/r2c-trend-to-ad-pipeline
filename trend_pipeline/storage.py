"""
storage.py — Structured persistence for VALIDATED signals (PRD: "structured
schema with full source attribution and timestamps").

Two interchangeable backends, selected by config.STORAGE_BACKEND:
  * JSON   — human-readable, good for demos and diffing
  * SQLite — queryable, what a production deployment would use

Week 2 reads validated signals from here to ground content generation, so the
stored record is the contract between the two weeks. Keep it stable.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config
from .gate1_quality import QualityVerdict

logger = logging.getLogger("storage")


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------
def build_validated_record(verdict: QualityVerdict) -> Dict[str, Any]:
    """Serialise a passing verdict into the Week 1 validated-signal schema."""
    signal = verdict.signal
    platforms: List[str] = list(getattr(verdict, "platforms_detected", []) or [])
    return {
        "signal_id": signal.signal_id,
        "validation_status": "VALIDATED",
        "gate_1_metrics": {
            "is_spam": verdict.is_spam,
            "is_duplicate": verdict.is_duplicate,
            "credibility_score": verdict.credibility_score,
            "credibility_breakdown": verdict.credibility_breakdown,
            "freshness_score": verdict.freshness_score,
            "age_hours": round(verdict.age_hours, 2) if verdict.age_hours is not None else None,
            "cross_platform_confirmed": bool(getattr(verdict, "cross_platform_confirmed", False)),
            "platforms_detected": platforms,
            "corroboration_group": getattr(verdict, "corroboration_group", None),
            "user_selected_source_exception": bool(
                getattr(verdict, "user_selected_source_exception", False)
            ),
        },
        "content": {
            "title_or_summary": signal.raw_content.title or signal.raw_content.body[:140],
            "raw_text": signal.text,
            "keywords": verdict.keywords,
            "content_hash": verdict.content_hash,
        },
        "source_attribution": {
            "primary_platform": signal.source_platform,
            "source_url": signal.source_url,
            "author_id": signal.author.author_id,
            "author_account_age_days": signal.author.account_age_days,
            "author_follower_count": signal.author.follower_count,
            "timestamp": signal.timestamp,
        },
        "engagement_metrics": signal.engagement_metrics.model_dump(),
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


def build_rejection_record(verdict: QualityVerdict) -> Dict[str, Any]:
    """Rejections are persisted too — Week 3's learning loop consumes them."""
    signal = verdict.signal
    return {
        "signal_id": signal.signal_id,
        "validation_status": verdict.status,
        "rejection_reason": verdict.reason,
        "source_platform": signal.source_platform,
        "source_url": signal.source_url,
        "timestamp": signal.timestamp,
        "credibility_score": verdict.credibility_score,
        "freshness_score": verdict.freshness_score,
        "excerpt": signal.text[:180],
        "stored_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# JSON backend
# ---------------------------------------------------------------------------
def _ensure_data_dir() -> None:
    os.makedirs(config.DATA_DIR, exist_ok=True)


def write_json(path: str, payload: Any) -> None:
    _ensure_data_dir()
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    logger.info("STORAGE | wrote %s", path)


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        logger.error("STORAGE | could not read %s: %s", path, exc)
        return default


# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS validated_signals (
    signal_id                TEXT PRIMARY KEY,
    validation_status        TEXT NOT NULL,
    source_platform          TEXT NOT NULL,
    source_url               TEXT,
    author_id                TEXT,
    posted_at                TEXT,
    credibility_score        REAL,
    freshness_score          REAL,
    cross_platform_confirmed INTEGER,
    platforms_detected       TEXT,
    corroboration_group      TEXT,
    title_or_summary         TEXT,
    raw_text                 TEXT,
    keywords                 TEXT,
    content_hash             TEXT,
    record_json              TEXT NOT NULL,
    stored_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validated_platform ON validated_signals(source_platform);
CREATE INDEX IF NOT EXISTS idx_validated_group    ON validated_signals(corroboration_group);
CREATE INDEX IF NOT EXISTS idx_validated_hash     ON validated_signals(content_hash);

CREATE TABLE IF NOT EXISTS rejected_signals (
    signal_id         TEXT PRIMARY KEY,
    validation_status TEXT NOT NULL,
    rejection_reason  TEXT,
    source_platform   TEXT,
    excerpt           TEXT,
    stored_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ranked_trends (
    trend_id    TEXT PRIMARY KEY,
    rank        INTEGER,
    topic_title TEXT,
    score       REAL,
    record_json TEXT NOT NULL,
    stored_at   TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    _ensure_data_dir()
    conn = sqlite3.connect(config.SIGNALS_DB)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _sqlite_upsert_validated(conn: sqlite3.Connection, records: Sequence[Dict[str, Any]]) -> None:
    rows = []
    for record in records:
        metrics = record["gate_1_metrics"]
        content = record["content"]
        attribution = record["source_attribution"]
        rows.append(
            (
                record["signal_id"],
                record["validation_status"],
                attribution["primary_platform"],
                attribution["source_url"],
                attribution["author_id"],
                attribution["timestamp"],
                metrics["credibility_score"],
                metrics["freshness_score"],
                1 if metrics["cross_platform_confirmed"] else 0,
                json.dumps(metrics["platforms_detected"]),
                metrics["corroboration_group"],
                content["title_or_summary"],
                content["raw_text"],
                json.dumps(content["keywords"]),
                content["content_hash"],
                json.dumps(record, ensure_ascii=False),
                record["stored_at"],
            )
        )
    conn.executemany(
        """INSERT INTO validated_signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(signal_id) DO UPDATE SET
               validation_status=excluded.validation_status,
               credibility_score=excluded.credibility_score,
               freshness_score=excluded.freshness_score,
               cross_platform_confirmed=excluded.cross_platform_confirmed,
               platforms_detected=excluded.platforms_detected,
               record_json=excluded.record_json,
               stored_at=excluded.stored_at""",
        rows,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def save_validated_signals(verdicts: Sequence[QualityVerdict]) -> List[Dict[str, Any]]:
    records = [build_validated_record(v) for v in verdicts]
    backend = config.STORAGE_BACKEND

    if backend in ("json", "both"):
        write_json(config.VALIDATED_SIGNALS_JSON, records)
    if backend in ("sqlite", "both"):
        with _connect() as conn:
            _sqlite_upsert_validated(conn, records)
        logger.info("STORAGE | upserted %d validated signals into %s",
                    len(records), config.SIGNALS_DB)

    logger.info("STORAGE | %d VALIDATED signals persisted (backend=%s)", len(records), backend)
    return records


def save_rejected_signals(verdicts: Sequence[QualityVerdict]) -> List[Dict[str, Any]]:
    records = [build_rejection_record(v) for v in verdicts]
    if config.STORAGE_BACKEND in ("json", "both"):
        write_json(config.REJECTED_SIGNALS_JSON, records)
    if config.STORAGE_BACKEND in ("sqlite", "both"):
        with _connect() as conn:
            conn.executemany(
                """INSERT INTO rejected_signals VALUES (?,?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                       validation_status=excluded.validation_status,
                       rejection_reason=excluded.rejection_reason,
                       stored_at=excluded.stored_at""",
                [
                    (
                        r["signal_id"], r["validation_status"], r["rejection_reason"],
                        r["source_platform"], r["excerpt"], r["stored_at"],
                    )
                    for r in records
                ],
            )
    logger.info("STORAGE | %d rejected signals archived for the Week 3 learning loop",
                len(records))
    return records


def load_validated_signals() -> List[Dict[str, Any]]:
    """Gate 2 entry point — read back what Gate 1 validated."""
    if config.STORAGE_BACKEND in ("json", "both"):
        records = read_json(config.VALIDATED_SIGNALS_JSON, default=[]) or []
        if records:
            logger.info("STORAGE | loaded %d validated signals from JSON", len(records))
            return records
    if config.STORAGE_BACKEND in ("sqlite", "both"):
        with _connect() as conn:
            rows = conn.execute("SELECT record_json FROM validated_signals").fetchall()
        records = [json.loads(row["record_json"]) for row in rows]
        logger.info("STORAGE | loaded %d validated signals from SQLite", len(records))
        return records
    return []


def save_ranked_trends(trends: Sequence[Dict[str, Any]]) -> None:
    if config.STORAGE_BACKEND in ("json", "both"):
        write_json(config.RANKED_TRENDS_JSON, list(trends))
    if config.STORAGE_BACKEND in ("sqlite", "both"):
        now = datetime.now(timezone.utc).isoformat()
        with _connect() as conn:
            conn.executemany(
                """INSERT INTO ranked_trends VALUES (?,?,?,?,?,?)
                   ON CONFLICT(trend_id) DO UPDATE SET
                       rank=excluded.rank, score=excluded.score,
                       record_json=excluded.record_json, stored_at=excluded.stored_at""",
                [
                    (
                        t["trend_id"], t["rank"], t["topic_title"],
                        t["composite_trend_score"], json.dumps(t, ensure_ascii=False), now,
                    )
                    for t in trends
                ],
            )
    logger.info("STORAGE | %d ranked trends persisted", len(trends))


# ---------------------------------------------------------------------------
# Post history (feeds Gate 1 near-duplicate detection)
# ---------------------------------------------------------------------------
def load_post_history() -> List[Tuple[str, str]]:
    """Prior signals/posts as (identifier, text) pairs for dedup seeding."""
    history = read_json(config.POST_HISTORY_JSON, default=[]) or []
    pairs: List[Tuple[str, str]] = []
    for item in history:
        if isinstance(item, dict):
            identifier = item.get("id") or item.get("signal_id") or "history"
            text = item.get("text") or item.get("raw_text") or ""
            if text:
                pairs.append((identifier, text))
        elif isinstance(item, str):
            pairs.append(("history", item))
    if pairs:
        logger.info("STORAGE | loaded %d items of post history for dedup", len(pairs))
    return pairs


def append_post_history(records: Sequence[Dict[str, Any]], limit: int = 500) -> None:
    """Roll this run's validated text into the history window for the next run."""
    history = read_json(config.POST_HISTORY_JSON, default=[]) or []
    known = {item.get("id") for item in history if isinstance(item, dict)}
    for record in records:
        if record["signal_id"] in known:
            continue
        history.append(
            {
                "id": record["signal_id"],
                "text": record["content"]["raw_text"],
                "stored_at": record["stored_at"],
            }
        )
    write_json(config.POST_HISTORY_JSON, history[-limit:])


def reset_store(remove_history: bool = False) -> None:
    """Clear generated artefacts so a demo run starts from a known state."""
    targets = [
        config.VALIDATED_SIGNALS_JSON,
        config.REJECTED_SIGNALS_JSON,
        config.RANKED_TRENDS_JSON,
        config.SIGNALS_DB,
    ]
    if remove_history:
        targets.append(config.POST_HISTORY_JSON)
    for path in targets:
        if os.path.exists(path):
            os.remove(path)
            logger.info("STORAGE | removed %s", path)


def storage_stats() -> Dict[str, Optional[int]]:
    if not os.path.exists(config.SIGNALS_DB):
        return {"validated": None, "rejected": None, "trends": None}
    with _connect() as conn:
        return {
            "validated": conn.execute("SELECT COUNT(*) c FROM validated_signals").fetchone()["c"],
            "rejected": conn.execute("SELECT COUNT(*) c FROM rejected_signals").fetchone()["c"],
            "trends": conn.execute("SELECT COUNT(*) c FROM ranked_trends").fetchone()["c"],
        }
