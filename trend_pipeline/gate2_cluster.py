"""
gate2_cluster.py — GATE 2 (step 1): topic clustering.

Embeds each validated signal's lead (title plus the first few hundred
characters) with the Gemini embedding model and groups the signals into topic
clusters in three passes:

  1. Average-linkage agglomeration. Two clusters merge while the MEAN pairwise
     cosine between their members clears CLUSTER_SIMILARITY_THRESHOLD. Unlike
     single-pass "join the nearest member" assignment, a chain of loosely
     similar posts cannot drag two distinct stories into one blob, and the
     result does not depend on input order.
  2. Same-story merge. Cluster pairs whose centroids sit in the ambiguous band
     (>= CLUSTER_MERGE_CANDIDATE_COSINE but below the linkage threshold) are
     shown to Gemini, which merges only those reporting the same specific
     story. Different wordings of one release or incident end up together.
  3. Cross-platform rule. Gate 1's "seen on >= 2 platforms" check is applied
     to these semantic clusters (see gate1_cross.py), so a story confirmed on
     two platforms is one that really appeared on two platforms.

Each surviving cluster then gets a Gemini-written title, summary and list of
key facts, plus the named entities its headlines share.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import config, text_utils
from .gemini_client import GeminiClient, get_client

logger = logging.getLogger("gate2.cluster")


@dataclass
class TopicCluster:
    """A group of validated signals judged to be about the same topic."""

    cluster_id: str
    signals: List[Dict[str, Any]] = field(default_factory=list)
    vectors: List[List[float]] = field(default_factory=list)
    centroid: List[float] = field(default_factory=list)
    topic_title: str = ""
    topic_summary: str = ""
    key_facts: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)

    # -- convenience views used by the scorer ------------------------------
    @property
    def size(self) -> int:
        return len(self.signals)

    @property
    def signal_ids(self) -> List[str]:
        return [s["signal_id"] for s in self.signals]

    @property
    def platforms(self) -> List[str]:
        seen: List[str] = []
        for signal in self.signals:
            platform = signal["source_attribution"]["primary_platform"]
            if platform not in seen:
                seen.append(platform)
        return seen

    @property
    def platform_count(self) -> int:
        return len(self.platforms)

    @property
    def confirmed(self) -> bool:
        """Gate 1 hard rule: the story was seen on >= MIN_CROSS_PLATFORM_COUNT platforms."""
        return self.platform_count >= config.MIN_CROSS_PLATFORM_COUNT

    @property
    def texts(self) -> List[str]:
        return [s["content"]["raw_text"] for s in self.signals]

    @property
    def label(self) -> str:
        return self.topic_title or ", ".join(self.keywords[:4]) or "(no keywords)"

    def add(self, signal: Dict[str, Any], vector: List[float]) -> None:
        self.signals.append(signal)
        self.vectors.append(vector)
        self._recompute_centroid()

    def absorb(self, other: "TopicCluster") -> None:
        self.signals.extend(other.signals)
        self.vectors.extend(other.vectors)
        self._recompute_centroid()

    def _recompute_centroid(self) -> None:
        if not self.vectors:
            self.centroid = []
            return
        dim = len(self.vectors[0])
        self.centroid = [
            sum(vec[i] for vec in self.vectors) / len(self.vectors) for i in range(dim)
        ]


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------
def _embedding_text(record: Dict[str, Any]) -> str:
    """The lead only: raw_text already starts with the title, then the body."""
    text = " ".join((record["content"].get("raw_text") or "").split())
    if not text:
        text = " ".join((record["content"].get("title_or_summary") or "").split())
    return text[: config.CLUSTER_EMBED_CHARS]


def embed_signals(
    records: Sequence[Dict[str, Any]], client: Optional[GeminiClient] = None
) -> List[List[float]]:
    client = client or get_client()
    texts = [_embedding_text(r) for r in records]
    logger.info("GATE2 | embedding %d validated signals (%s, lead <= %d chars)", len(texts),
                config.GEMINI_EMBEDDING_MODEL if client.online else "offline fallback",
                config.CLUSTER_EMBED_CHARS)
    return client.embed_texts(texts)


# ---------------------------------------------------------------------------
# Pass 1 — average-linkage agglomerative clustering
# ---------------------------------------------------------------------------
def _gemini_vectors(client: GeminiClient) -> bool:
    """
    True if the last embeddings really came from Gemini.

    Checks what embed_texts() actually returned, not just whether the client is
    online: a quota error mid-run falls back to lexical vectors, and applying the
    Gemini threshold to those splits every story into singletons.
    """
    mode = getattr(client, "embedding_mode", None)
    return client.online if mode is None else mode == "gemini"


def similarity_threshold(client: Optional[GeminiClient] = None) -> float:
    """Gemini and lexical vectors occupy different cosine ranges - see config."""
    client = client or get_client()
    return (
        config.CLUSTER_SIMILARITY_THRESHOLD
        if _gemini_vectors(client)
        else config.CLUSTER_SIMILARITY_THRESHOLD_LEXICAL
    )


def merge_candidate_cosine(client: Optional[GeminiClient] = None) -> float:
    client = client or get_client()
    return (
        config.CLUSTER_MERGE_CANDIDATE_COSINE
        if _gemini_vectors(client)
        else config.CLUSTER_MERGE_CANDIDATE_COSINE_LEXICAL
    )


def cluster_signals(
    records: Sequence[Dict[str, Any]],
    vectors: Sequence[Sequence[float]],
    threshold: Optional[float] = None,
) -> List[TopicCluster]:
    """
    Average-linkage agglomerative clustering.

    Start with one cluster per signal and repeatedly merge the pair with the
    highest mean pairwise cosine, stopping when that mean drops below the
    threshold. Average linkage sits between single linkage (which chains) and
    complete linkage (which fragments): a signal joins a story only if it is
    similar to the story as a whole, not just to its nearest member.

    Ties are broken by index so the result is deterministic, and because the
    merge order is driven by similarity values rather than arrival order, the
    output does not depend on the order signals were ingested in.
    """
    if not records:
        return []
    if threshold is None:
        threshold = similarity_threshold()
    logger.info("GATE2 | average-linkage clustering at cosine threshold %.2f", threshold)

    n = len(records)
    vectors = [list(v) for v in vectors]
    # Running cluster-to-cluster mean similarity (Lance-Williams update below
    # keeps it exact for average linkage without re-reading member vectors).
    avg = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            avg[i][j] = avg[j][i] = text_utils.cosine_dense(vectors[i], vectors[j])

    active: Set[int] = set(range(n))
    members: List[List[int]] = [[i] for i in range(n)]
    best_of: Dict[int, Tuple[float, int]] = {}

    def recompute(i: int) -> None:
        best = (-2.0, -1)
        for j in active:
            if j != i and (avg[i][j] > best[0] or (avg[i][j] == best[0] and j < best[1])):
                best = (avg[i][j], j)
        best_of[i] = best

    for i in active:
        recompute(i)

    while len(active) > 1:
        a = min(active, key=lambda i: (-best_of[i][0], i))
        value, b = best_of[a]
        if b < 0 or value < threshold:
            break
        a, b = min(a, b), max(a, b)
        size_a, size_b = len(members[a]), len(members[b])
        for c in active:
            if c in (a, b):
                continue
            avg[a][c] = avg[c][a] = (size_a * avg[a][c] + size_b * avg[b][c]) / (size_a + size_b)
        members[a].extend(members[b])
        active.remove(b)
        del best_of[b]
        logger.debug("GATE2 | merged %s into %s (mean cosine %.3f)",
                     records[members[b][0]]["signal_id"], records[members[a][0]]["signal_id"], value)
        # Average linkage keeps the new similarity between the old two values,
        # so only clusters whose best partner was a or b need a fresh search.
        for c in list(active):
            if c == a or best_of[c][1] in (a, b):
                recompute(c)

    ordered = sorted((members[i] for i in active), key=lambda m: (-len(m), min(m)))
    clusters: List[TopicCluster] = []
    for index, member_ids in enumerate(ordered, start=1):
        cluster = TopicCluster(cluster_id="cls_{:03d}".format(index))
        for member in sorted(member_ids):
            cluster.add(records[member], vectors[member])
        cluster.keywords = _cluster_keywords(cluster)
        clusters.append(cluster)

    logger.info("GATE2 | formed %d topic cluster(s) from %d signals", len(clusters), len(records))
    return clusters


def _cluster_keywords(cluster: TopicCluster, top_k: int = 8) -> List[str]:
    counts: Counter = Counter()
    for signal in cluster.signals:
        counts.update(signal["content"].get("keywords") or [])
    if not counts:
        counts.update(text_utils.extract_keywords(" ".join(cluster.texts), top_k=top_k))
    return [kw for kw, _ in counts.most_common(top_k)]


def _renumber(clusters: Sequence[TopicCluster]) -> List[TopicCluster]:
    ordered = sorted(clusters, key=lambda c: (-c.size, c.signal_ids[0] if c.signals else ""))
    for index, cluster in enumerate(ordered, start=1):
        cluster.cluster_id = "cls_{:03d}".format(index)
    return ordered


# ---------------------------------------------------------------------------
# Pass 2 — Gemini same-story merge
# ---------------------------------------------------------------------------
MERGE_PROMPT = """You are de-duplicating topic clusters built from AI/ML news, research feeds and social posts.
Each line is one cluster: its id, the platforms it was seen on, and up to two representative headlines.

{items}

Which clusters report the SAME specific story: the same model release, paper, product launch, security incident, funding deal or announcement?
Be strict. Merge only when the headlines describe the same event, by the same organisation, about the same named thing.
NOT the same story:
- two different model releases, even from the same lab or of the same size;
- the same model in two different stories (e.g. a benchmark result about a model vs. a fine-tuned or uncensored variant of it vs. someone deploying it);
- different version numbers (V4.1 vs V4, K3.1 vs K3);
- a news story and a general explainer, opinion piece or tutorial on the same subject;
- two unrelated stories that share a theme such as "AI agents" or "AI security".
When unsure, do not merge.

Reply with JSON only: a list of lists of cluster ids to merge, for example [["C1","C4"],["C2","C7","C9"]].
Reply [] if nothing should be merged."""


def _representative_titles(cluster: TopicCluster, limit: int = 2) -> List[str]:
    candidates = []
    for signal in cluster.signals:
        text = " ".join((signal["content"].get("title_or_summary") or "").split())
        if len(text) >= 20:
            candidates.append(_shorten(text, 110))
    candidates = list(dict.fromkeys(candidates))
    headline_like = sorted((c for c in candidates if len(c) <= 110), key=len)
    return headline_like[:limit] or [_shorten(cluster.texts[0], 110)]


def _parse_id_groups(reply: Optional[str], valid: Set[str]) -> List[List[str]]:
    if not reply:
        return []
    start, end = reply.find("["), reply.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        parsed = json.loads(reply[start:end + 1])
    except ValueError:
        return []
    groups: List[List[str]] = []
    for group in parsed if isinstance(parsed, list) else []:
        if not isinstance(group, list):
            continue
        ids = list(dict.fromkeys(str(item).strip().upper() for item in group if str(item).strip()))
        ids = [i for i in ids if i in valid]
        if len(ids) >= 2:
            groups.append(ids)
    return groups


def merge_same_story_clusters(
    clusters: Sequence[TopicCluster],
    client: Optional[GeminiClient] = None,
    candidate_cosine: Optional[float] = None,
) -> List[TopicCluster]:
    """
    Ask Gemini which near-neighbour clusters are the same story, and merge them.

    Only cluster pairs whose centroids clear `candidate_cosine` are candidates,
    and a merge is honoured only along such pairs, so the model can decide
    "same story or not" but cannot fuse topics the embeddings call unrelated.
    Offline (no Gemini) this is a no-op.
    """
    client = client or get_client()
    clusters = list(clusters)
    if len(clusters) < 2 or not client.online:
        return clusters
    floor = merge_candidate_cosine(client) if candidate_cosine is None else candidate_cosine

    close: Dict[int, Set[int]] = {}
    for i in range(len(clusters)):
        for j in range(i + 1, len(clusters)):
            if text_utils.cosine_dense(clusters[i].centroid, clusters[j].centroid) >= floor:
                close.setdefault(i, set()).add(j)
                close.setdefault(j, set()).add(i)
    if not close:
        logger.info("GATE2 | same-story merge: no cluster pairs within cosine %.2f", floor)
        return clusters

    ids = {"C{}".format(i): i for i in sorted(close)}
    items = "\n".join(
        "{}: [{}] {}".format(cid, ", ".join(clusters[i].platforms),
                             " | ".join(_representative_titles(clusters[i])))
        for cid, i in ids.items()
    )
    reply = client.generate_text(MERGE_PROMPT.format(items=items), label="cluster_merge")
    groups = _parse_id_groups(reply, set(ids))

    parent = list(range(len(clusters)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    merges = 0
    for group in groups:
        indices = [ids[cid] for cid in group]
        for a in indices:
            for b in indices:
                # Honour the model only along pairs the embeddings call close.
                if a < b and b in close.get(a, ()) and find(a) != find(b):
                    parent[find(b)] = find(a)
                    merges += 1
                    logger.info("GATE2 | same-story merge: %s + %s (%s)", clusters[a].cluster_id,
                                clusters[b].cluster_id, _representative_titles(clusters[b], 1)[0])
    if not merges:
        logger.info("GATE2 | same-story merge: Gemini kept all %d candidate clusters separate", len(ids))
        return clusters

    merged: Dict[int, TopicCluster] = {}
    for index, cluster in enumerate(clusters):
        root = find(index)
        if root in merged:
            merged[root].absorb(cluster)
        else:
            merged[root] = cluster
    result = _renumber(list(merged.values()))
    for cluster in result:
        cluster.keywords = _cluster_keywords(cluster)
    logger.info("GATE2 | same-story merge: %d cluster(s) -> %d", len(clusters), len(result))
    return result


# ---------------------------------------------------------------------------
# Named entities shared by a cluster's headlines
# ---------------------------------------------------------------------------
_ENTITY_TOKEN_RE = re.compile(r"[A-Za-z0-9][\w.\-]*")
# Words that are capitalised in headlines but are not part of a name. They end
# the current phrase ("Five Qwen3.8-27B Models Match Claude Fable 5 Using GVS5H"
# -> "Qwen3.8-27B", "Claude Fable 5", "GVS5H") and never start or join one.
_ENTITY_BREAKERS = {
    "a", "an", "the", "this", "that", "these", "those", "how", "why", "what", "when",
    "where", "who", "which", "new", "major", "breaking", "here", "top", "big", "report",
    "reports", "says", "say", "said", "inside", "meet", "introducing", "first", "latest",
    "unvetted", "terrifying", "global", "critical", "key", "best", "all", "every",
    "some", "more", "most", "just", "now", "today", "week", "discussion", "update",
    "updated", "via", "with", "without", "from", "for", "and", "or", "but", "of", "on",
    "in", "to", "at", "by", "as", "is", "are", "was", "were", "be", "it", "its", "vs",
    "using", "uses", "use", "match", "matches", "beats", "tops", "launch", "launches",
    "launched", "release", "releases", "released", "introduce", "introduces", "unveils",
    "announces", "announced", "adds", "gets", "makes", "lets", "plans", "raises",
    "model", "models", "agent", "agents", "app", "tool", "tools", "harness", "paper",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "why", "can", "could", "will", "would", "should", "may", "might", "after", "before",
    "over", "under", "into", "about", "against", "between", "show", "hn", "ask",
}
# Single words that are too generic to be an entity on their own.
_ENTITY_GENERIC = {"ai", "llm", "llms", "ml", "gpu", "api", "github", "open", "source", "us", "uk"}


def _has_digit(token: str) -> bool:
    return any(ch.isdigit() for ch in token)


def _entity_phrases(title: str) -> List[str]:
    """
    Split a headline into candidate names: runs of capitalised or alphanumeric
    tokens, broken by connector and verb words. Digit-only tokens may continue
    a name ("Claude Fable 5") but never start one ("40 Modes").
    """
    tokens = _ENTITY_TOKEN_RE.findall(title or "")
    phrases: List[str] = []
    current: List[str] = []

    def flush() -> None:
        if current:
            phrases.append(" ".join(current))
        current.clear()

    for raw in tokens:
        token = raw.strip(".-")
        if not token:
            continue
        lowered = token.lower()
        is_named = token[0].isupper() or (_has_digit(token) and any(ch.isalpha() for ch in token))
        continues = bool(current) and token.isdigit()
        if lowered in _ENTITY_BREAKERS or not (is_named or continues):
            flush()
            continue
        if current and current[-1].lower() == lowered:
            continue  # "GVS5H GVS5H" from a repo title
        current.append(token)
        if len(current) == 4:
            flush()
    flush()
    return [
        p for p in phrases
        if p and p.lower() not in _ENTITY_GENERIC and (len(p) > 2 or _has_digit(p))
    ]


def extract_entities(cluster: TopicCluster, limit: int = 6) -> List[str]:
    """
    Proper nouns, product and model names shared by the cluster's headlines.

    Keyword lists come out as "model, llm, release"; the entity list comes out
    as "DeepSeek V4.1 Flash, Moonshot AI" - what a writer can actually hook on.
    """
    counts: Counter = Counter()
    for signal in cluster.signals:
        title = signal["content"].get("title_or_summary") or ""
        seen_here = set()
        for phrase in _entity_phrases(title):
            key = phrase.lower()
            if key in seen_here:
                continue
            seen_here.add(key)
            counts[key] += 1
    if not counts:
        return []
    display: Dict[str, str] = {}
    for signal in cluster.signals:
        for phrase in _entity_phrases(signal["content"].get("title_or_summary") or ""):
            display.setdefault(phrase.lower(), phrase)

    def rank(item: Tuple[str, int]) -> Tuple[float, str]:
        key, count = item
        words = key.split()
        score = count * (1.0 + 0.5 * (len(words) - 1))
        if _has_digit(key):
            score += 1.0  # model and product names carry versions: K3.1, V4.1, 27B
        if len(words) == 1 and count < 2 and not _has_digit(key):
            score -= 1.5  # a lone capitalised word seen once is usually a sentence start
        return (-score, key)

    ranked = [display[key] for key, _ in sorted(counts.items(), key=rank)]
    # Drop a phrase contained in a higher-ranked one ("Kimi" under "Kimi K3.1 Pro").
    kept: List[str] = []
    for phrase in ranked:
        lowered = phrase.lower()
        if any(lowered in other.lower().split() or lowered in other.lower() for other in kept):
            continue
        kept.append(phrase)
    return kept[:limit]


# ---------------------------------------------------------------------------
# Thin-cluster and cross-platform filters
# ---------------------------------------------------------------------------
def drop_thin_clusters(
    clusters: Sequence[TopicCluster], minimum_size: Optional[int] = None
) -> List[TopicCluster]:
    """A lone mention is not a topic — enforce MIN_CLUSTER_SIZE."""
    minimum_size = config.MIN_CLUSTER_SIZE if minimum_size is None else minimum_size
    kept, dropped = [], []
    for cluster in clusters:
        (kept if cluster.size >= minimum_size else dropped).append(cluster)
    for cluster in dropped:
        logger.info(
            "GATE2 | %s DROPPED (only %d signal(s) < MIN_CLUSTER_SIZE %d) | %s",
            cluster.cluster_id, cluster.size, minimum_size,
            ", ".join(cluster.keywords[:4]),
        )
    return kept


def drop_single_platform_clusters(
    clusters: Sequence[TopicCluster],
) -> Tuple[List[TopicCluster], List[TopicCluster]]:
    """Gate 1's >= 2 platform rule, applied to semantic clusters. Returns (kept, dropped)."""
    kept = [c for c in clusters if c.confirmed]
    dropped = [c for c in clusters if not c.confirmed]
    for cluster in dropped:
        logger.info(
            "GATE2 | %s SINGLE-SOURCE (%d signal(s) on %s only) | %s",
            cluster.cluster_id, cluster.size, ", ".join(cluster.platforms) or "no platform",
            ", ".join(cluster.keywords[:4]),
        )
    return kept, dropped


# ---------------------------------------------------------------------------
# Summarisation: title, summary and key facts
# ---------------------------------------------------------------------------
SUMMARY_PROMPT = """You are analysing social, news and research signals about one topic for a research-focused AI company.

Signals (platform, then text):
{excerpts}

Write exactly these sections, each on its own line(s), with no other text:
TITLE: a specific 4-9 word headline naming this topic (name the model, company, paper or event)
SUMMARY: 2-3 sentences describing what happened and why it matters to AI/ML practitioners.
KEY_FACTS:
- up to 5 bullet lines, each one specific, checkable fact from the signals: a number, name, date, benchmark, price or quote. Tag each with the platform it came from in square brackets.
Ground everything strictly in the signals above. Do not speculate or add outside knowledge."""

MAX_TITLE_CHARS = 72
SUMMARY_SIGNALS = 10
SUMMARY_EXCERPT_CHARS = 700


def _shorten(text: str, limit: int = MAX_TITLE_CHARS) -> str:
    """Trim to a sentence end if one fits, else to a clean word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text.rstrip(" .,;:-")
    for stop in (". ", "? ", "! ", "; ", " - ", ", "):
        head = text.find(stop)
        if 25 <= head <= limit:
            return text[:head].rstrip(" .,;:-")
    cut = text.rfind(" ", 0, limit)
    return text[: cut if cut > 25 else limit].rstrip(" .,;:-")


def _fallback_title(cluster: TopicCluster) -> str:
    """
    Pick the most headline-like text available when Gemini is not summarising.

    Platforms with a real title field (Reddit, Facebook, Google Search) give a
    short, self-contained headline; X and Instagram only give post bodies, which
    storage truncates. So prefer the SHORTEST candidate long enough to be
    meaningful - that is almost always a genuine title rather than a post body.
    """
    candidates = [
        " ".join((s["content"].get("title_or_summary") or "").split())
        for s in cluster.signals
    ]
    candidates = [c for c in candidates if len(c) >= 20]
    if not candidates:
        return " ".join(kw.title() for kw in cluster.keywords[:5]) or cluster.cluster_id

    headline_like = [c for c in candidates if len(c) <= 90]
    if headline_like:
        return _shorten(min(headline_like, key=len))
    return _shorten(min(candidates, key=len))


def _fallback_summary(cluster: TopicCluster) -> str:
    return (
        "{} signals across {} discuss {}. "
        "Corroborated by {} validated source(s) in this cycle."
    ).format(
        cluster.size,
        ", ".join(cluster.platforms),
        ", ".join(cluster.entities[:3] or cluster.keywords[:5]) or "this topic",
        cluster.size,
    )


def _summary_excerpts(cluster: TopicCluster) -> str:
    """Longest texts first: articles carry the facts, short posts the reaction."""
    ranked = sorted(cluster.signals, key=lambda s: -len(s["content"].get("raw_text") or ""))
    return "\n".join(
        "- [{}] {}".format(
            signal["source_attribution"]["primary_platform"],
            " ".join((signal["content"].get("raw_text") or "").split())[:SUMMARY_EXCERPT_CHARS],
        )
        for signal in ranked[:SUMMARY_SIGNALS]
    )


def summarise_clusters(
    clusters: Sequence[TopicCluster], client: Optional[GeminiClient] = None
) -> None:
    """Attach topic_title, topic_summary, key_facts and entities to each cluster, in place."""
    client = client or get_client()
    for cluster in clusters:
        cluster.entities = extract_entities(cluster)
        reply = client.generate_text(
            SUMMARY_PROMPT.format(excerpts=_summary_excerpts(cluster)), label="cluster_summary"
        )
        title, summary, facts = _parse_summary_reply(reply)
        cluster.topic_title = title or _fallback_title(cluster)
        cluster.topic_summary = summary or _fallback_summary(cluster)
        cluster.key_facts = facts
        logger.info(
            "GATE2 | %s | %d signals | %s | %s | %d fact(s)",
            cluster.cluster_id, cluster.size, cluster.platforms, cluster.topic_title,
            len(cluster.key_facts),
        )


def _parse_summary_reply(reply: Optional[str]) -> Tuple[str, str, List[str]]:
    if not reply:
        return "", "", []
    title, summary, facts = "", "", []
    section = ""
    for line in reply.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("TITLE:"):
            title, section = stripped[6:].strip(), "title"
        elif upper.startswith("SUMMARY:"):
            summary, section = stripped[8:].strip(), "summary"
        elif upper.startswith("KEY_FACTS") or upper.startswith("KEY FACTS"):
            section = "facts"
        elif section == "facts" and stripped[:1] in ("-", "*", "•"):
            fact = stripped.lstrip("-*• ").strip()
            if fact:
                facts.append(fact)
        elif section == "summary" and stripped:
            summary = "{} {}".format(summary, stripped).strip()
    if not title and not summary:
        summary = reply.strip()
    return title.strip("*# "), summary, facts[:5]


# ---------------------------------------------------------------------------
# Stage entry points
# ---------------------------------------------------------------------------
def build_clusters(
    records: Sequence[Dict[str, Any]], client: Optional[GeminiClient] = None
) -> List[TopicCluster]:
    """Embed, agglomerate and same-story-merge. Keeps every cluster, singletons included."""
    client = client or get_client()
    records = list(records)
    if not records:
        return []
    vectors = embed_signals(records, client=client)
    clusters = cluster_signals(records, vectors, threshold=similarity_threshold(client))
    return merge_same_story_clusters(clusters, client=client)


def refresh_cluster_records(
    clusters: Sequence[TopicCluster], records: Sequence[Dict[str, Any]]
) -> None:
    """Swap in freshly stored records (same signal_ids) so clusters carry the saved annotations."""
    by_id = {r["signal_id"]: r for r in records}
    for cluster in clusters:
        cluster.signals = [by_id.get(s["signal_id"], s) for s in cluster.signals]


def run_clustering(
    records: Optional[Sequence[Dict[str, Any]]] = None,
    client: Optional[GeminiClient] = None,
    minimum_cluster_size: Optional[int] = None,
    require_cross_platform: bool = True,
) -> List[TopicCluster]:
    """Load validated signals (unless supplied), cluster them, filter them, summarise them."""
    from . import storage  # local import keeps the module import graph acyclic

    client = client or get_client()
    records = list(records) if records is not None else storage.load_validated_signals()
    logger.info("GATE2 | ---- topic clustering over %d validated signals ----", len(records))
    if not records:
        logger.warning("GATE2 | no validated signals to cluster — Gate 2 has nothing to score.")
        return []

    clusters = build_clusters(records, client=client)
    if require_cross_platform:
        clusters, _dropped = drop_single_platform_clusters(clusters)
    clusters = drop_thin_clusters(clusters, minimum_size=minimum_cluster_size)
    summarise_clusters(clusters, client=client)
    return clusters
