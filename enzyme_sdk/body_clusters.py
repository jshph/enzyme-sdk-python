"""Entry clustering for structured entries before ingest."""

from __future__ import annotations

import copy
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from enzyme_sdk.client import EnzymeError

if TYPE_CHECKING:
    from enzyme_sdk.client import EnzymeClient


_TEXT_FIELDS = ("title", "content", "notes", "body", "comment", "text", "description")
_SOURCE_KEYWORD_FIELDS = ("keywords", "labels", "categories", "ingredients", "tags_source")
_METADATA_KEYWORD_FIELDS = ("keywords", "categories", "ingredients")
_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "but",
    "can",
    "for",
    "from",
    "had",
    "has",
    "have",
    "her",
    "his",
    "into",
    "its",
    "more",
    "not",
    "our",
    "out",
    "over",
    "she",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "was",
    "were",
    "with",
    "you",
    "your",
}


EntryInput = dict[str, Any] | str
TextBuilder = Callable[[EntryInput], str]
Granularity = Literal["balanced", "fine"]


@dataclass
class ClusterRepresentative:
    title: str
    text: str
    similarity: float
    entry_id: str | None = None


@dataclass
class EntryCluster:
    id: str
    embedding: list[float]
    keywords: list[str]
    size: int
    cohesion: float
    medoid: ClusterRepresentative | None
    representatives: list[ClusterRepresentative]

    @property
    def tag(self) -> str:
        return self.id


@dataclass
class EntryClusterAssignment:
    entry_index: int
    entry_id: str
    cluster_id: str
    similarity: float


@dataclass
class EntryClusterAssignmentResult:
    entries: list[dict[str, Any]]
    assignments: list[EntryClusterAssignment]
    clusters: list[EntryCluster]


@dataclass
class EntryClusterSingleAssignmentResult:
    entry: dict[str, Any]
    assignments: list[EntryClusterAssignment]
    clusters: list[EntryCluster]


@dataclass
class EntryClusterIndex:
    model: str
    dimension: int
    id_prefix: str
    assignment_min_similarity: float
    clusters: list[EntryCluster]
    client: "EnzymeClient | None" = None

    def assign(
        self,
        entries: list[EntryInput],
        *,
        text: TextBuilder | None = None,
        min_similarity: float | None = None,
        max_clusters_per_entry: int = 1,
        target_field: str = "tags",
    ) -> EntryClusterAssignmentResult:
        """Assign entries to stored clusters and append flat cluster tags."""
        if self.client is None:
            raise EnzymeError("EntryClusterIndex.assign requires a client")
        if max_clusters_per_entry < 1:
            raise ValueError("max_clusters_per_entry must be at least 1")
        if not target_field:
            raise ValueError("target_field must not be empty")

        np = _load_numpy()
        normalized_entries = _normalize_entries(entries)
        embedding_entries = _embedding_payload(normalized_entries, text=text)
        enriched_entries = copy.deepcopy(normalized_entries)
        if not enriched_entries or not self.clusters:
            return EntryClusterAssignmentResult(
                entries=enriched_entries,
                assignments=[],
                clusters=self.clusters,
            )

        embedded = self.client.embed_entries(embedding_entries)
        items = _extract_items(embedded, len(normalized_entries))
        normalized = _normalize_vectors(np, [item.get("vector") for item in items])
        cluster_matrix = _normalize_vectors(np, [cluster.embedding for cluster in self.clusters])
        similarities = normalized @ cluster_matrix.T
        threshold = self.assignment_min_similarity if min_similarity is None else min_similarity

        assignments: list[EntryClusterAssignment] = []
        for entry_index in range(len(normalized_entries)):
            ranked_cluster_indices = sorted(
                range(len(self.clusters)),
                key=lambda cluster_index: (
                    -float(similarities[entry_index, cluster_index]),
                    self.clusters[cluster_index].id,
                ),
            )
            selected = 0
            for cluster_index in ranked_cluster_indices:
                similarity = float(similarities[entry_index, cluster_index])
                if similarity < threshold:
                    continue
                cluster = self.clusters[cluster_index]
                _append_value(enriched_entries[entry_index], target_field, cluster.id)
                assignments.append(
                    EntryClusterAssignment(
                        entry_index=entry_index,
                        entry_id=_entry_id(normalized_entries[entry_index], entry_index),
                        cluster_id=cluster.id,
                        similarity=similarity,
                    )
                )
                selected += 1
                if selected >= max_clusters_per_entry:
                    break

        return EntryClusterAssignmentResult(
            entries=enriched_entries,
            assignments=assignments,
            clusters=self.clusters,
        )

    def assign_one(
        self,
        entry: EntryInput,
        *,
        text: TextBuilder | None = None,
        min_similarity: float | None = None,
        max_clusters_per_entry: int = 1,
        target_field: str = "tags",
    ) -> EntryClusterSingleAssignmentResult:
        result = self.assign(
            [entry],
            text=text,
            min_similarity=min_similarity,
            max_clusters_per_entry=max_clusters_per_entry,
            target_field=target_field,
        )
        return EntryClusterSingleAssignmentResult(
            entry=result.entries[0],
            assignments=result.assignments,
            clusters=result.clusters,
        )

    def assign_text(
        self,
        body: str,
        *,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
        min_similarity: float | None = None,
        max_clusters_per_entry: int = 1,
        target_field: str = "tags",
    ) -> EntryClusterSingleAssignmentResult:
        entry: dict[str, Any] = {"text": body}
        if title is not None:
            entry["title"] = title
        if metadata is not None:
            entry["metadata"] = metadata
        return self.assign_one(
            entry,
            min_similarity=min_similarity,
            max_clusters_per_entry=max_clusters_per_entry,
            target_field=target_field,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dimension": self.dimension,
            "id_prefix": self.id_prefix,
            "assignment_min_similarity": self.assignment_min_similarity,
            "clusters": [asdict(cluster) for cluster in self.clusters],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        client: "EnzymeClient | None" = None,
    ) -> "EntryClusterIndex":
        clusters = [_cluster_from_dict(cluster) for cluster in data.get("clusters", [])]
        return cls(
            model=str(data.get("model", "")),
            dimension=int(data.get("dimension", 0)),
            id_prefix=str(data.get("id_prefix", "auto-cluster")),
            assignment_min_similarity=float(data.get("assignment_min_similarity", 0.60)),
            clusters=clusters,
            client=client,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(
        cls,
        path: str | Path,
        *,
        client: "EnzymeClient | None" = None,
    ) -> "EntryClusterIndex":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")), client=client)


# Compatibility aliases.
BodyCluster = EntryCluster
BodyClusterResult = EntryClusterAssignmentResult


def build_entry_cluster_index(
    entries: list[EntryInput],
    *,
    client: "EnzymeClient | None" = None,
    text: TextBuilder | None = None,
    id_prefix: str = "auto-cluster",
    granularity: Granularity = "balanced",
    k: int | None = None,
    min_similarity: float | None = None,
    min_cluster_size: int | None = None,
    resolution: float | None = None,
    seed: int = 13,
) -> EntryClusterIndex:
    """Build a reusable index of automatic entry clusters."""
    np, igraph, leidenalg = _load_cluster_deps()

    if client is None:
        from enzyme_sdk.client import EnzymeClient

        client = EnzymeClient()

    normalized_entries = _normalize_entries(entries)
    analysis_entries = copy.deepcopy(normalized_entries)
    if text is not None:
        for entry in analysis_entries:
            entry["text"] = _embedding_text(entry, text=text)
    embedding_entries = _embedding_payload(analysis_entries)
    embedded = client.embed_entries(embedding_entries)
    items = _extract_items(embedded, len(normalized_entries))
    model = str(embedded.get("model", ""))
    dimension = int(embedded.get("dimension") or (len(items[0].get("vector") or []) if items else 0))

    if not normalized_entries:
        return EntryClusterIndex(
            model=model,
            dimension=dimension,
            id_prefix=id_prefix,
            assignment_min_similarity=min_similarity or 0.60,
            clusters=[],
            client=client,
        )

    normalized = _normalize_vectors(np, [item.get("vector") for item in items])
    similarities = normalized @ normalized.T
    n = len(normalized_entries)
    low_level_overrides = any(
        value is not None
        for value in (k, min_similarity, min_cluster_size, resolution)
    )
    if low_level_overrides:
        params = {
            "k": k or 8,
            "min_similarity": min_similarity if min_similarity is not None else 0.60,
            "min_cluster_size": min_cluster_size or 5,
            "resolution": resolution if resolution is not None else 0.35,
        }
    else:
        params = _select_cluster_params(
            np,
            igraph,
            leidenalg,
            similarities,
            granularity=granularity,
            seed=seed,
        )

    communities = _cluster_communities(
        np,
        igraph,
        leidenalg,
        similarities,
        k=params["k"],
        min_similarity=params["min_similarity"],
        min_cluster_size=params["min_cluster_size"],
        resolution=params["resolution"],
        seed=seed,
    )
    clusters = _build_clusters(
        np,
        normalized,
        similarities,
        analysis_entries,
        communities,
        id_prefix,
    )

    return EntryClusterIndex(
        model=model,
        dimension=dimension,
        id_prefix=id_prefix,
        assignment_min_similarity=params["min_similarity"],
        clusters=clusters,
        client=client,
    )


def cluster_entries(
    entries: list[EntryInput],
    *,
    client: "EnzymeClient | None" = None,
    **kwargs: Any,
) -> EntryClusterAssignmentResult:
    target_field = kwargs.pop("target_field", "tags")
    index = build_entry_cluster_index(entries, client=client, **kwargs)
    return index.assign(
        entries,
        text=kwargs.get("text"),
        target_field=target_field,
    )


def cluster_body_entries(
    entries: list[EntryInput],
    *,
    client: "EnzymeClient | None" = None,
    tag_prefix: str | None = None,
    **kwargs: Any,
) -> EntryClusterAssignmentResult:
    if tag_prefix is not None and "id_prefix" not in kwargs:
        kwargs["id_prefix"] = tag_prefix
    return cluster_entries(entries, client=client, **kwargs)


def _select_cluster_params(
    np,
    igraph,
    leidenalg,
    similarities,
    *,
    granularity: Granularity,
    seed: int,
) -> dict[str, Any]:
    n = int(similarities.shape[0])
    candidates = _cluster_param_candidates(n, granularity)
    scored: list[tuple[float, dict[str, Any]]] = []
    for params in candidates:
        communities = _cluster_communities(
            np,
            igraph,
            leidenalg,
            similarities,
            k=params["k"],
            min_similarity=params["min_similarity"],
            min_cluster_size=params["min_cluster_size"],
            resolution=params["resolution"],
            seed=seed,
        )
        score = _score_communities(
            np,
            similarities,
            communities,
            granularity=granularity,
            min_cluster_size=params["min_cluster_size"],
        )
        scored.append((score, params))

    scored.sort(
        key=lambda item: (
            -item[0],
            item[1]["min_similarity"],
            item[1]["resolution"],
            item[1]["k"],
        )
    )
    return scored[0][1]


# Sweep guidance for future agents:
#
# This selector is intentionally a lightweight heuristic, not a claim of
# optimal clustering. Embeddings dominate cost, so the public API embeds once
# and compares several graph/community settings against the same similarity
# matrix. When changing these candidates, compare at least these metrics across
# multiple corpora and corpus sizes:
#
# - cluster_count: should land near sqrt(n), bounded to a human-readable range.
#   Too few clusters means the automatic tags do not explain much; too many
#   clusters make tags noisy and brittle. `fine` should usually produce more
#   clusters than `balanced`.
# - coverage: share of entries that receive an automatic cluster. Balanced
#   should usually tag a useful minority of entries, not 5% and not 100%.
#   Fine is allowed to cover more, especially for sparse user-level corpora.
# - cohesion: average pairwise similarity inside each retained cluster, weighted
#   by cluster size. This prevents the sweep from winning purely by coverage.
# - fragmentation: many clusters just above `min_cluster_size` often indicate
#   over-splitting or accidental local neighborhoods.
# - giant clusters: one cluster containing a large share of the corpus usually
#   means the similarity threshold is too loose or the graph is too connected.
#
# Important limitation: the current score measures compactness but not true
# between-cluster separation or stability. Better future selectors should add
# a separation metric such as silhouette-style nearest-cluster comparison and a
# stability metric from subsampling/perturbing the neighbor graph. HDBSCAN-style
# density clustering is also a plausible replacement because it treats
# unclustered points as noise and selects clusters by stability, which matches
# the product goal that only genuinely recurring patterns should become tags.
def _cluster_param_candidates(n: int, granularity: Granularity) -> list[dict[str, Any]]:
    if granularity not in {"balanced", "fine"}:
        raise ValueError("granularity must be 'balanced' or 'fine'")

    base_k = _default_k(n)
    min_size = _default_min_cluster_size(n, granularity)
    by_granularity = {
        "balanced": [
            {"resolution": 0.25, "min_similarity": 0.62},
            {"resolution": 0.35, "min_similarity": 0.60},
            {"resolution": 0.45, "min_similarity": 0.58},
            {"resolution": 0.60, "min_similarity": 0.56},
            {"resolution": 0.80, "min_similarity": 0.54},
            {"resolution": 1.00, "min_similarity": 0.52},
        ],
        "fine": [
            {"resolution": 0.45, "min_similarity": 0.60},
            {"resolution": 0.60, "min_similarity": 0.58},
            {"resolution": 0.80, "min_similarity": 0.56},
            {"resolution": 1.00, "min_similarity": 0.54},
            {"resolution": 1.20, "min_similarity": 0.52},
            {"resolution": 1.40, "min_similarity": 0.50},
        ],
    }

    candidates: list[dict[str, Any]] = []
    for params in by_granularity[granularity]:
        candidate = dict(params)
        candidate["k"] = base_k
        candidate["min_cluster_size"] = min_size
        candidates.append(candidate)
    if n < 50:
        candidates.append({
            "k": base_k,
            "min_cluster_size": min_size,
            "resolution": 1.20,
            "min_similarity": 0.45,
        })
    return candidates


def _default_k(n: int) -> int:
    return max(2, min(24, int(round(math.sqrt(max(1, n))))))


def _default_min_cluster_size(n: int, granularity: Granularity) -> int:
    if n < 12:
        return 2
    if n < 40:
        return 2
    if n < 500:
        if granularity == "fine":
            return 4
        return 5
    if granularity == "fine":
        return max(4, min(6, int(round(math.sqrt(n) / 6))))
    return max(4, min(12, int(round(math.sqrt(n) / 2))))


def _target_cluster_range(n: int, granularity: Granularity) -> tuple[float, float]:
    root = math.sqrt(max(1, n))
    if granularity == "fine":
        target = root * 1.35
    else:
        target = root
    center = max(2.0, min(30.0, target))
    return max(1.0, center * 0.55), min(45.0, center * 1.45)


def _target_coverage_range(granularity: Granularity) -> tuple[float, float]:
    if granularity == "fine":
        return 0.35, 0.80
    return 0.30, 0.70


def _range_score(value: float, low: float, high: float) -> float:
    if low <= value <= high:
        return 1.0
    if value < low:
        return max(0.0, value / low) if low > 0 else 0.0
    return max(0.0, 1.0 - ((value - high) / max(high, 1.0)))


def _score_communities(
    np,
    similarities,
    communities: list[list[int]],
    *,
    granularity: Granularity,
    min_cluster_size: int,
) -> float:
    n = int(similarities.shape[0])
    if n == 0:
        return 0.0

    cluster_count = len(communities)
    covered = sum(len(indices) for indices in communities)
    coverage = covered / n
    count_low, count_high = _target_cluster_range(n, granularity)
    coverage_low, coverage_high = _target_coverage_range(granularity)

    cluster_count_score = _range_score(cluster_count, count_low, count_high)
    coverage_score = _range_score(coverage, coverage_low, coverage_high)
    cohesion_score = _weighted_cohesion(np, similarities, communities)
    fragmentation_penalty = _fragmentation_penalty(communities, min_cluster_size)
    giant_cluster_penalty = _giant_cluster_penalty(n, communities)

    return (
        (1.2 * cluster_count_score)
        + (1.4 * coverage_score)
        + (1.0 * cohesion_score)
        - (0.7 * fragmentation_penalty)
        - (1.2 * giant_cluster_penalty)
    )


def _weighted_cohesion(np, similarities, communities: list[list[int]]) -> float:
    weighted_total = 0.0
    weight = 0
    for indices in communities:
        if len(indices) < 2:
            continue
        pairwise = similarities[np.ix_(indices, indices)]
        upper = pairwise[np.triu_indices(len(indices), k=1)]
        if not upper.size:
            continue
        weighted_total += float(upper.mean()) * len(indices)
        weight += len(indices)
    return weighted_total / weight if weight else 0.0


def _fragmentation_penalty(
    communities: list[list[int]],
    min_cluster_size: int,
) -> float:
    if not communities:
        return 0.0
    tiny_limit = max(min_cluster_size + 1, int(math.ceil(min_cluster_size * 1.35)))
    tiny = sum(1 for indices in communities if len(indices) <= tiny_limit)
    return tiny / len(communities)


def _giant_cluster_penalty(n: int, communities: list[list[int]]) -> float:
    if not communities or n == 0:
        return 0.0
    largest = max(len(indices) for indices in communities) / n
    if largest <= 0.25:
        return 0.0
    return min(1.0, (largest - 0.25) / 0.50)


def _cluster_communities(
    np,
    igraph,
    leidenalg,
    similarities,
    *,
    k: int,
    min_similarity: float,
    min_cluster_size: int,
    resolution: float,
    seed: int,
) -> list[list[int]]:
    neighbors = _top_k_neighbors(np, similarities, k)
    edges, weights = _mutual_edges(similarities, neighbors, min_similarity)

    graph = igraph.Graph(n=int(similarities.shape[0]), edges=edges, directed=False)
    graph.es["weight"] = weights

    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        seed=seed,
    )

    communities: list[list[int]] = []
    for community in partition:
        indices = sorted(int(i) for i in community)
        if len(indices) >= min_cluster_size:
            communities.append(indices)
    communities.sort(key=lambda indices: (indices[0], len(indices)))
    return communities


def _build_clusters(
    np,
    normalized,
    similarities,
    analysis_entries: list[dict[str, Any]],
    communities: list[list[int]],
    id_prefix: str,
) -> list[EntryCluster]:
    clusters: list[EntryCluster] = []
    used_slugs: Counter[str] = Counter()
    for fallback_number, indices in enumerate(communities, 1):
        cluster_matrix = normalized[indices]
        centroid = cluster_matrix.mean(axis=0)
        centroid_norm = float(np.linalg.norm(centroid))
        if centroid_norm > 0:
            centroid = centroid / centroid_norm

        scores = cluster_matrix @ centroid
        score_by_entry = {entry_index: float(scores[offset]) for offset, entry_index in enumerate(indices)}
        ranked_indices = sorted(indices, key=lambda entry_index: (-score_by_entry[entry_index], entry_index))
        representatives = [
            _representative(analysis_entries[entry_index], entry_index, score_by_entry[entry_index])
            for entry_index in ranked_indices[:5]
        ]
        keywords = _cluster_keywords(analysis_entries, indices, ranked_indices)
        cluster_id = _cluster_id(id_prefix, keywords, fallback_number, used_slugs)

        if len(indices) > 1:
            pairwise = similarities[np.ix_(indices, indices)]
            upper = pairwise[np.triu_indices(len(indices), k=1)]
            cohesion = float(upper.mean()) if upper.size else 0.0
        else:
            cohesion = 0.0

        clusters.append(
            EntryCluster(
                id=cluster_id,
                embedding=[float(value) for value in centroid.tolist()],
                keywords=keywords,
                size=len(indices),
                cohesion=cohesion,
                medoid=representatives[0] if representatives else None,
                representatives=representatives,
            )
        )
    return clusters


def _load_numpy():
    try:
        import numpy as np
    except ImportError as e:
        raise EnzymeError(
            "Entry clustering requires optional dependencies. "
            "Install them with: pip install 'enzyme-sdk[cluster]'"
        ) from e
    return np


def _load_cluster_deps():
    try:
        import igraph
        import leidenalg
        import numpy as np
    except ImportError as e:
        raise EnzymeError(
            "Entry clustering requires optional dependencies. "
            "Install them with: pip install 'enzyme-sdk[cluster]'"
        ) from e
    return np, igraph, leidenalg


def _extract_items(embedded: dict[str, Any], expected_count: int) -> list[dict[str, Any]]:
    items = embedded.get("items", [])
    if len(items) != expected_count:
        raise EnzymeError(f"Embed entries returned {len(items)} items for {expected_count} entries")
    return items


def _normalize_entries(entries: list[EntryInput]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append({"text": entry})
        elif isinstance(entry, dict):
            normalized.append(copy.deepcopy(entry))
        else:
            raise TypeError("entries must contain dicts or strings")
    return normalized


def _embedding_payload(
    entries: list[dict[str, Any]],
    *,
    text: TextBuilder | None = None,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        embedding_entry: dict[str, Any] = {
            "id": _entry_id(entry, index),
            "text": _embedding_text(entry, text=text),
        }
        if entry.get("title"):
            embedding_entry["title"] = str(entry["title"])
        payload.append(embedding_entry)
    return payload


def _embedding_text(entry: dict[str, Any], *, text: TextBuilder | None = None) -> str:
    if text is not None:
        try:
            value = text(entry)
        except Exception as e:
            raise EnzymeError(f"Entry text builder failed: {e}") from e
        if value is None:
            raise EnzymeError("Entry text builder returned None")
        return str(value)

    parts: list[str] = []
    for field in _TEXT_FIELDS:
        value = entry.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            text = value
        elif isinstance(value, (list, tuple, set)):
            text = " ".join(str(item) for item in value if item)
        else:
            text = str(value)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _normalize_vectors(np, vectors: list[Any]):
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2:
        raise EnzymeError("Embed entries returned vectors with an invalid shape")

    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)


def _top_k_neighbors(np, similarities, k: int) -> list[set[int]]:
    n = similarities.shape[0]
    if n == 0:
        return []

    neighbors: list[set[int]] = []
    k = max(0, min(k, n - 1))
    for i in range(n):
        if k == 0:
            neighbors.append(set())
            continue

        scores = similarities[i].copy()
        scores[i] = -np.inf
        candidate_indices = np.argpartition(-scores, kth=k - 1)[:k]
        candidate_indices = sorted(candidate_indices, key=lambda j: (-float(scores[j]), int(j)))
        neighbors.append(set(int(j) for j in candidate_indices))
    return neighbors


def _mutual_edges(similarities, neighbors: list[set[int]], min_similarity: float):
    edges: list[tuple[int, int]] = []
    weights: list[float] = []
    for i, entry_neighbors in enumerate(neighbors):
        for j in entry_neighbors:
            if i >= j or i not in neighbors[j]:
                continue
            similarity = float(similarities[i, j])
            if similarity >= min_similarity:
                edges.append((i, j))
                weights.append(similarity)
    return edges, weights


def _append_value(entry: dict[str, Any], field: str, value: str) -> None:
    values = entry.get(field)
    if values is None:
        entry[field] = [value]
        return

    if isinstance(values, list):
        if value not in values:
            values.append(value)
        return

    if isinstance(values, tuple | set):
        normalized = list(values)
    else:
        normalized = [values]
    if value not in normalized:
        normalized.append(value)
    entry[field] = normalized


def _entry_id(entry: dict[str, Any], index: int) -> str:
    for key in ("id", "source", "source_ref", "url", "title"):
        value = entry.get(key)
        if value:
            return str(value)
    return f"entry-{index}"


def _representative(
    entry: dict[str, Any],
    entry_index: int,
    similarity: float,
) -> ClusterRepresentative:
    entry_id = _entry_id(entry, entry_index)
    portable_id = entry_id if any(entry.get(key) for key in ("id", "source", "source_ref", "url", "title")) else None
    title = str(entry.get("title") or portable_id or "Untitled")
    return ClusterRepresentative(
        title=title,
        text=_short_entry_text(entry),
        similarity=float(similarity),
        entry_id=portable_id,
    )


def _short_entry_text(entry: dict[str, Any], limit: int = 420) -> str:
    parts = [str(entry[field]) for field in _TEXT_FIELDS if entry.get(field)]
    text = " ".join(parts)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip() + "..."


def _cluster_keywords(
    entries: list[dict[str, Any]],
    indices: list[int],
    ranked_indices: list[int],
) -> list[str]:
    source_terms = _source_keywords(entries, indices)
    if source_terms:
        return source_terms[:4]

    cluster_counts: Counter[str] = Counter()
    outside_counts: Counter[str] = Counter()
    cluster_set = set(indices)
    for index, entry in enumerate(entries):
        words = set(_entry_words(entry))
        target = cluster_counts if index in cluster_set else outside_counts
        target.update(words)

    scored: list[tuple[float, str]] = []
    outside_total = max(1, len(entries) - len(indices))
    cluster_total = max(1, len(indices))
    for word, count in cluster_counts.items():
        if len(word) < 3:
            continue
        cluster_rate = count / cluster_total
        outside_rate = outside_counts[word] / outside_total
        score = cluster_rate * math.log((1.0 + cluster_total + outside_total) / (1.0 + outside_counts[word]))
        score += max(0.0, cluster_rate - outside_rate)
        scored.append((score, word))

    scored.sort(key=lambda item: (-item[0], item[1]))
    terms = [word for _, word in scored[:4]]
    if terms:
        return terms

    fallback_terms: list[str] = []
    for entry_index in ranked_indices:
        for word in _words(str(entries[entry_index].get("title") or "")):
            if word not in fallback_terms:
                fallback_terms.append(word)
            if len(fallback_terms) >= 4:
                return fallback_terms
    return fallback_terms


def _source_keywords(entries: list[dict[str, Any]], indices: list[int]) -> list[str]:
    counts: Counter[str] = Counter()
    for index in indices:
        entry = entries[index]
        for field in _SOURCE_KEYWORD_FIELDS:
            counts.update(_keyword_values(entry.get(field)))
        metadata = entry.get("metadata")
        if isinstance(metadata, dict):
            for field in _METADATA_KEYWORD_FIELDS:
                counts.update(_keyword_values(metadata.get(field)))
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [term for term, _ in ordered if _slug_words(term)][:4]


def _keyword_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [str(key) for key, enabled in value.items() if enabled]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return [str(value)]


def _entry_words(entry: dict[str, Any]) -> list[str]:
    return _words(" ".join(str(entry[field]) for field in _TEXT_FIELDS if entry.get(field)))


def _words(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-z0-9]+", normalized.lower())
    return [word for word in words if word not in _STOPWORDS and not word.isdigit()]


def _cluster_id(
    id_prefix: str,
    keywords: list[str],
    fallback_number: int,
    used_slugs: Counter[str],
) -> str:
    slug_words: list[str] = []
    for keyword in keywords:
        for word in _slug_words(keyword):
            if word not in slug_words:
                slug_words.append(word)
            if len(slug_words) >= 4:
                break
        if len(slug_words) >= 4:
            break

    slug = "-".join(slug_words)
    if not slug:
        slug = f"c{fallback_number:03d}"
    slug = slug[:48].rstrip("-") or f"c{fallback_number:03d}"

    used_slugs[slug] += 1
    if used_slugs[slug] > 1:
        slug = f"{slug}-{used_slugs[slug]}"
    return f"{_clean_id_prefix(id_prefix)}-{slug}"


def _clean_id_prefix(id_prefix: str) -> str:
    normalized = unicodedata.normalize("NFKD", id_prefix).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", normalized.lower()).strip("-")
    return normalized or "auto-cluster"


def _slug_words(text: str) -> list[str]:
    return _words(text)


def _cluster_from_dict(data: dict[str, Any]) -> EntryCluster:
    representatives = [
        ClusterRepresentative(**representative)
        for representative in data.get("representatives", [])
    ]
    medoid_data = data.get("medoid")
    medoid = ClusterRepresentative(**medoid_data) if isinstance(medoid_data, dict) else None
    return EntryCluster(
        id=str(data["id"]),
        embedding=[float(value) for value in data.get("embedding", [])],
        keywords=[str(keyword) for keyword in data.get("keywords", [])],
        size=int(data.get("size", 0)),
        cohesion=float(data.get("cohesion", 0.0)),
        medoid=medoid,
        representatives=representatives,
    )
