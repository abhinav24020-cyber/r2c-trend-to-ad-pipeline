"""
gate1_cross.py — GATE 1 (part B): cross-platform corroboration.

PRD hard rule: a topic must be corroborated on >= 2 platforms before any of its
signals may be treated as a validated trend input. A signal whose topic appears
on exactly one platform is rejected as REJECT_SINGLE_SOURCE.

The rule is applied to Gate 2's semantic topic clusters (gate2_cluster.build_clusters),
not to a separate lexical grouping. The earlier keyword/TF-IDF union-find
grouping linked any two AI stories that shared vocabulary, so on live data one
group swallowed most of the batch and the rule confirmed almost everything.
Grouping once, semantically, means "seen on two platforms" is decided on the
same clusters that are later scored and ranked.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Sequence, Tuple

from . import config
from .gate1_quality import REJECT_SINGLE_SOURCE, QualityVerdict
from .gate2_cluster import TopicCluster, drop_single_platform_clusters

logger = logging.getLogger("gate1.cross")


def run_cross_platform_validation(
    verdicts: Sequence[QualityVerdict],
    clusters: Sequence[TopicCluster],
) -> Tuple[List[QualityVerdict], List[QualityVerdict], List[TopicCluster]]:
    """
    Apply the >= 2 platform rule to clustered Gate 1 survivors.

    Returns (validated, rejected_single_source, confirmed_clusters). Each verdict
    is annotated in place with cross_platform_confirmed / platforms_detected /
    corroboration_group so storage.py can persist the evidence; rejected
    verdicts get status REJECT_SINGLE_SOURCE and a reason.
    """
    logger.info(
        "GATE1 | ---- part B: cross-platform corroboration (>= %d platforms) on %d topic cluster(s) ----",
        config.MIN_CROSS_PLATFORM_COUNT, len(clusters),
    )
    if not verdicts:
        return [], [], []

    confirmed, single = drop_single_platform_clusters(clusters)
    by_signal: Dict[str, TopicCluster] = {
        sid: cluster for cluster in clusters for sid in cluster.signal_ids
    }

    validated: List[QualityVerdict] = []
    rejected: List[QualityVerdict] = []
    for verdict in verdicts:
        cluster = by_signal.get(verdict.signal.signal_id)
        platforms = cluster.platforms if cluster else [verdict.signal.source_platform]
        verdict.corroboration_group = cluster.cluster_id if cluster else None
        verdict.platforms_detected = platforms
        verdict.cross_platform_confirmed = bool(cluster and cluster.confirmed)
        if verdict.cross_platform_confirmed:
            validated.append(verdict)
            continue
        verdict.status = REJECT_SINGLE_SOURCE
        verdict.reason = "topic found on {} platform ({}) only; needs >= {}".format(
            len(platforms), ", ".join(platforms) or "unknown", config.MIN_CROSS_PLATFORM_COUNT
        )
        logger.info(
            "GATE1 | %-24s | %-22s | %s",
            verdict.signal.signal_id, REJECT_SINGLE_SOURCE, verdict.reason,
        )
        rejected.append(verdict)

    for cluster in confirmed:
        logger.info(
            "GATE1 | %-7s | CONFIRMED    | %d signal(s) on %d platform(s) %s | %s",
            cluster.cluster_id, cluster.size, cluster.platform_count, cluster.platforms,
            ", ".join(cluster.entities[:3] or cluster.keywords[:4]),
        )
    logger.info(
        "GATE1 | part B complete: %d VALIDATED in %d confirmed topic(s), %d rejected as single-source (%d topic(s))",
        len(validated), len(confirmed), len(rejected), len(single),
    )
    return validated, rejected, confirmed
