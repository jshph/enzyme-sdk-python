"""Unit tests for app/user scoped hosted search client behavior."""

from __future__ import annotations

import json

import httpx

from enzyme_sdk.hosted import HostedScopeClient


def _client(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(
        transport=transport,
        base_url="https://search.test/v1/scopes/chat-app/user-123",
    )
    return HostedScopeClient(
        api_key="test-key",
        app_id="chat-app",
        user_id="user-123",
        base_url="https://search.test",
        http_client=http_client,
    )


def test_scope_catalyze_posts_scope_request_without_collections():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "scope": "chat-app/user-123",
                "scope_fingerprint": "fp-1",
                "query": "customer handoff",
                "register": "explore",
                "catalysts": [
                    {
                        "entity": "Avery",
                        "text": "Avery appears in product handoff threads.",
                        "relevance": 0.92,
                        "contribution_count": 3,
                        "collection_id": "internal-messages",
                    }
                ],
                "results": [
                    {
                        "primitive": "message",
                        "source_id": "msg-1",
                        "title": "Launch handoff",
                        "snippet": "Avery summarized the customer constraint.",
                        "app_url": "app://messages/msg-1",
                        "created_at": "2026-04-29T10:00:00Z",
                        "metadata": {"thread_id": "thread-9"},
                        "similarity": 0.81,
                        "collection_id": "internal-messages",
                    }
                ],
                "total": 1,
            },
        )

    with _client(handler) as client:
        response = client.catalyze("customer handoff", limit=5)

    assert seen["path"] == "/v1/scopes/chat-app/user-123/catalyze"
    assert seen["body"] == {
        "query": "customer handoff",
        "limit": 5,
        "register": "explore",
        "debug": False,
    }
    assert "collections" not in seen["body"]
    assert response.scope == "chat-app/user-123"
    assert response.scope_fingerprint == "fp-1"
    assert response.catalysts[0].entity == "Avery"
    assert response.catalysts[0].contribution_count == 3
    assert not hasattr(response.catalysts[0], "collection_id")
    assert response.results[0].primitive == "message"
    assert response.results[0].source_id == "msg-1"
    assert response.results[0].metadata == {"thread_id": "thread-9"}
    assert not hasattr(response.results[0], "collection_id")


def test_scope_catalyze_preserves_debug_cache_when_requested():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["debug"] is True
        return httpx.Response(
            200,
            json={
                "scope": "chat-app/user-123",
                "query": "design review",
                "catalysts": [],
                "results": [],
                "total": 0,
                "cache": {
                    "scope_fingerprint": "fp-2",
                    "routes": [
                        {
                            "collection_id": "internal-artifacts",
                            "index_generation": 4,
                        }
                    ],
                },
            },
        )

    with _client(handler) as client:
        response = client.catalyze("design review", debug=True)

    assert response.debug == {
        "scope_fingerprint": "fp-2",
        "routes": [
            {
                "collection_id": "internal-artifacts",
                "index_generation": 4,
            }
        ],
    }


def test_scope_status_exposes_internal_collection_health():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/scopes/chat-app/user-123/status"
        return httpx.Response(
            200,
            json={
                "scope": "chat-app/user-123",
                "scope_fingerprint": "fp-3",
                "totals": {
                    "docs": 9,
                    "entities": 5,
                    "catalysts": 7,
                    "embeddings": 16,
                },
                "collections": [
                    {
                        "name": "messages",
                        "index_generation": 11,
                        "counts": {
                            "docs": 4,
                            "entities": 3,
                            "catalysts": 4,
                            "embeddings": 8,
                        },
                    }
                ],
            },
        )

    with _client(handler) as client:
        status = client.status()

    assert status.scope == "chat-app/user-123"
    assert status.totals["docs"] == 9
    assert status.collections[0].name == "messages"
    assert status.collections[0].index_generation == 11
    assert status.collections[0].counts["embeddings"] == 8


def test_scope_petri_normalizes_legacy_and_structured_catalysts():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/scopes/chat-app/user-123/petri"
        assert request.url.params["top"] == "2"
        assert request.url.params["query"] == "handoff"
        return httpx.Response(
            200,
            json={
                "entities": [
                    {
                        "name": "Avery",
                        "type": "person",
                        "frequency": 12,
                        "frequency_12m": 10,
                        "recency_score": 0.8,
                        "activity_trend": "rising",
                        "days_since_last_seen": 1,
                        "last_seen": 1777466400,
                        "catalysts": [
                            "Avery owns handoffs.",
                            {"text": "Avery appears with launch planning."},
                        ],
                    }
                ]
            },
        )

    with _client(handler) as client:
        entities = client.petri(top=2, query="handoff")

    assert entities[0].name == "Avery"
    assert entities[0].frequency_12m == 10
    assert entities[0].catalysts == [
        {"text": "Avery owns handoffs."},
        {"text": "Avery appears with launch planning."},
    ]


def test_scope_refresh_refreshes_whole_scope_without_collection_selector():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"status": "queued"})

    with _client(handler) as client:
        response = client.refresh()

    assert seen["path"] == "/v1/scopes/chat-app/user-123/refresh"
    assert seen["body"] == {}
    assert response == {"status": "queued"}
