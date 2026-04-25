import builtins
import copy
import math

import pytest

from enzyme_sdk.body_clusters import (
    EntryCluster,
    EntryClusterIndex,
    build_entry_cluster_index,
    cluster_body_entries,
)
from enzyme_sdk.client import EnzymeError


class FakeClient:
    vectors = {
        "rice-1": [1.0, 0.0, 0.0],
        "rice-2": [0.99, 0.01, 0.0],
        "rice-3": [0.98, 0.02, 0.0],
        "cake-1": [0.0, 1.0, 0.0],
        "cake-2": [0.0, 0.99, 0.01],
        "cake-3": [0.0, 0.98, 0.02],
        "other": [0.0, 0.0, 1.0],
    }

    def embed_entries(self, entries):
        assert all(isinstance(entry, dict) for entry in entries)
        assert all("text" in entry for entry in entries)
        return {
            "model": "fake",
            "dimension": 3,
            "items": [
                {"id": entry.get("id", f"entry-{index}"), "vector": self.vectors[entry["id"]]}
                for index, entry in enumerate(entries)
            ],
        }


class TextFakeClient(FakeClient):
    def embed_entries(self, entries):
        assert all(isinstance(entry, dict) for entry in entries)
        assert all(set(entry).issubset({"id", "text", "title"}) for entry in entries)
        items = []
        for index, entry in enumerate(entries):
            text = str(entry.get("text", "")).lower()
            if "cake" in text:
                vector = [0.0, 1.0, 0.0]
            elif "other" in text:
                vector = [0.0, 0.0, 1.0]
            else:
                vector = [1.0, 0.0, 0.0]
            items.append({"id": entry.get("id", f"entry-{index}"), "vector": vector})
        return {"model": "fake", "dimension": 3, "items": items}


class CountingTextFakeClient(TextFakeClient):
    def __init__(self):
        self.embed_calls = 0

    def embed_entries(self, entries):
        self.embed_calls += 1
        return super().embed_entries(entries)


def _entries():
    return [
        {"id": "rice-1", "title": "Black Rice Salad", "keywords": ["black rice"], "tags": ["existing"]},
        {"id": "rice-2", "title": "Black Rice Bowl", "keywords": ["black rice"]},
        {"id": "rice-3", "title": "Rice With Herbs", "keywords": ["black rice"]},
        {"id": "cake-1", "title": "Bundt Cake Glaze", "keywords": ["bundt cake"]},
        {"id": "cake-2", "title": "Chocolate Bundt Cake", "keywords": ["bundt cake"]},
        {"id": "cake-3", "title": "Lemon Cake Glaze", "keywords": ["bundt cake"]},
    ]


def test_build_entry_cluster_index_builds_normalized_reusable_clusters():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        _entries(),
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    assert index.model == "fake"
    assert index.dimension == 3
    assert len(index.clusters) == 2
    assert [cluster.id for cluster in index.clusters] == [
        "auto-cluster-black-rice",
        "auto-cluster-bundt-cake",
    ]
    assert all(
        math.isclose(
            sum(value * value for value in cluster.embedding),
            1.0,
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
        for cluster in index.clusters
    )
    assert index.clusters[0].medoid is not None
    assert index.clusters[0].medoid.entry_id == "rice-2"
    assert index.clusters[0].representatives == sorted(
        index.clusters[0].representatives,
        key=lambda representative: -representative.similarity,
    )


def test_assign_deep_copies_entries_and_appends_flat_tags():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    entries = _entries()
    original = copy.deepcopy(entries)
    index = build_entry_cluster_index(
        entries,
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    result = index.assign(
        [
            {"id": "rice-1", "title": "User rice", "tags": ["mine"]},
            {"id": "other", "title": "Unrelated"},
        ]
    )

    assert entries == original
    assert result.entries[0]["tags"] == ["mine", "auto-cluster-black-rice"]
    assert "/" not in result.entries[0]["tags"][-1]
    assert "tags" not in result.entries[1]
    assert len(result.assignments) == 1
    assert result.assignments[0].cluster_id == "auto-cluster-black-rice"


def test_assign_can_write_cluster_tags_to_target_field():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        _entries(),
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    result = index.assign(
        [{"id": "rice-1", "title": "User rice", "tags": ["source"]}],
        target_field="auto_tags",
    )

    assert result.entries[0]["tags"] == ["source"]
    assert result.entries[0]["auto_tags"] == ["auto-cluster-black-rice"]


def test_assign_assembles_embedding_text_without_changing_returned_entry():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        [
            "Black rice salad with herbs",
            "Black rice bowl with greens",
            "Rice and cabbage dinner",
        ],
        client=TextFakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    entry = {"title": "Dinner note", "body": "Black rice with greens", "metadata": {"kept": True}}
    result = index.assign([entry])

    assert result.entries[0] == {
        "title": "Dinner note",
        "body": "Black rice with greens",
        "metadata": {"kept": True},
        "tags": ["auto-cluster-rice-black-bowl-cabbage"],
    }
    assert "text" not in result.entries[0]


def test_text_lambda_controls_build_and_assign_text():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    rows = [
        {"recipe": "Black rice salad", "comment": "great with herbs"},
        {"recipe": "Black rice bowl", "comment": "good with greens"},
        {"recipe": "Rice and cabbage", "comment": "weeknight dinner"},
    ]
    index = build_entry_cluster_index(
        rows,
        client=TextFakeClient(),
        text=lambda row: f"{row['recipe']}\n\n{row['comment']}",
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    result = index.assign(
        [{"recipe": "Dinner note", "comment": "black rice and greens"}],
        text=lambda row: f"{row['recipe']}\n\n{row['comment']}",
    )

    assert index.clusters[0].id == "auto-cluster-rice-black-bowl-cabbage"
    assert result.entries[0] == {
        "recipe": "Dinner note",
        "comment": "black rice and greens",
        "tags": ["auto-cluster-rice-black-bowl-cabbage"],
    }


def test_raw_string_inputs_return_normalized_tagged_entries():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        [
            "Black rice salad with herbs",
            "Black rice bowl with greens",
            "Rice and cabbage dinner",
            "Bundt cake with glaze",
            "Chocolate cake slice",
            "Lemon cake for dessert",
        ],
        client=TextFakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    result = index.assign(["A new black rice dinner"])

    assert result.entries == [
        {"text": "A new black rice dinner", "tags": ["auto-cluster-rice-black-bowl-cabbage"]}
    ]
    assert result.assignments[0].entry_id == "entry-0"


def test_default_balanced_granularity_embeds_once():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    client = CountingTextFakeClient()
    index = build_entry_cluster_index(
        [
            "Black rice salad with herbs",
            "Black rice bowl with greens",
            "Rice and cabbage dinner",
            "Bundt cake with glaze",
            "Chocolate cake slice",
            "Lemon cake for dessert",
        ],
        client=client,
    )

    assert client.embed_calls == 1
    assert len(index.clusters) == 2
    assert any("rice" in cluster.id for cluster in index.clusters)
    assert any("cake" in cluster.id for cluster in index.clusters)
    assert index.assignment_min_similarity in {0.45, 0.52, 0.54, 0.56, 0.58, 0.60, 0.62}


def test_granularity_values_are_supported():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    for granularity in ("balanced", "fine"):
        index = build_entry_cluster_index(
            _entries(),
            client=FakeClient(),
            granularity=granularity,
        )
        assert index.clusters


def test_low_level_overrides_keep_legacy_defaults():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        _entries(),
        client=FakeClient(),
        min_similarity=0.9,
        min_cluster_size=3,
    )

    assert [cluster.id for cluster in index.clusters] == [
        "auto-cluster-black-rice",
        "auto-cluster-bundt-cake",
    ]
    assert index.assignment_min_similarity == 0.9


def test_assign_one_and_assign_text_helpers():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    index = build_entry_cluster_index(
        _entries(),
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    one = index.assign_one({"id": "cake-1", "title": "Fresh cake note"})
    assert one.entry["tags"] == ["auto-cluster-bundt-cake"]
    assert one.assignments[0].entry_index == 0

    text_index = build_entry_cluster_index(
        [
            "Black rice salad with herbs",
            "Black rice bowl with greens",
            "Rice and cabbage dinner",
        ],
        client=TextFakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )
    text = text_index.assign_text("Other topic", title="Other", metadata={"source": "test"})

    assert text.entry == {"text": "Other topic", "title": "Other", "metadata": {"source": "test"}}
    assert text.assignments == []


def test_max_clusters_per_entry_and_serialization_round_trip(tmp_path):
    pytest.importorskip("numpy")

    index = EntryClusterIndex(
        model="fake",
        dimension=3,
        id_prefix="auto-cluster",
        assignment_min_similarity=0.5,
        client=FakeClient(),
        clusters=[
            EntryCluster("auto-cluster-x", [1, 0, 0], ["x"], 1, 1.0, None, []),
            EntryCluster("auto-cluster-y", [0.8, 0.6, 0], ["y"], 1, 1.0, None, []),
        ],
    )
    path = tmp_path / "clusters.json"
    index.save(path)
    loaded = EntryClusterIndex.load(path, client=FakeClient())

    result = loaded.assign([{"id": "rice-1"}], max_clusters_per_entry=2)

    assert [assignment.cluster_id for assignment in result.assignments] == [
        "auto-cluster-x",
        "auto-cluster-y",
    ]
    assert result.entries[0]["tags"] == ["auto-cluster-x", "auto-cluster-y"]


def test_cluster_body_entries_alias_uses_flat_default_prefix():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    result = cluster_body_entries(
        _entries()[:3],
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    assert result.clusters[0].id == "auto-cluster-black-rice"
    assert result.entries[0]["tags"][-1] == "auto-cluster-black-rice"


def test_cluster_body_entries_passes_target_field_to_assignment():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    result = cluster_body_entries(
        _entries()[:3],
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
        target_field="auto_tags",
    )

    assert result.entries[0]["tags"] == ["existing"]
    assert result.entries[0]["auto_tags"] == ["auto-cluster-black-rice"]


def test_duplicate_keyword_slugs_get_deterministic_suffixes():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    entries = _entries()
    for entry in entries:
        entry["keywords"] = ["shared topic"]

    index = build_entry_cluster_index(
        entries,
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=3,
    )

    assert [cluster.id for cluster in index.clusters] == [
        "auto-cluster-shared-topic",
        "auto-cluster-shared-topic-2",
    ]


def test_tiny_clusters_are_not_tagged():
    pytest.importorskip("numpy")
    pytest.importorskip("igraph")
    pytest.importorskip("leidenalg")

    result = cluster_body_entries(
        _entries()[:3],
        client=FakeClient(),
        k=2,
        min_similarity=0.9,
        min_cluster_size=4,
    )

    assert result.clusters == []
    assert result.entries == _entries()[:3]


def test_cluster_body_entries_reports_missing_optional_dependencies(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "igraph":
            raise ImportError("no igraph")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(EnzymeError, match="enzyme-sdk\\[cluster\\]"):
        cluster_body_entries([{"id": "rice-1"}], client=FakeClient())
