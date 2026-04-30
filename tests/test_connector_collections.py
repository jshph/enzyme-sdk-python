"""Unit tests for connector-level item collection mapping."""

from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from enzyme_sdk import Activity, EnzymeConnector, enzyme


@dataclass
class CookingEvent:
    id: str
    kind: str
    recipe_name: str
    comment: str
    auto_tags: list[str]


def test_collection_hook_maps_typed_item_schema_to_collection_id():
    connector = EnzymeConnector(
        api_key="enz_test",
        app_id="nyt-cooking",
        display_name="NYT Cooking Notes",
        content_label="cooking activity",
    )

    @enzyme.collection(connector)
    def cooking_collection(event: CookingEvent) -> str:
        return event.kind

    @enzyme.on_save(
        connector,
        title="recipe_name",
        content="comment",
        tags="auto_tags",
        primitive="kind",
        source_id="id",
    )
    def save_event(user_id: str, event: CookingEvent) -> CookingEvent:
        return event

    event = CookingEvent(
        id="comment-1",
        kind="recipe_comment",
        recipe_name="Soy-Braised Tofu",
        comment="Good with extra ginger.",
        auto_tags=["weeknight", None, "ginger"],
    )

    connector._connected_users.add("user-123")
    save_event("user-123", event)

    assert connector.collection_for(event) == "recipe_comment"
    assert connector._user_stores["user-123"] == [
        {
            "title": "Soy-Braised Tofu",
            "content": "Good with extra ginger.",
            "tags": ["weeknight", "ginger"],
            "primitive": "recipe_comment",
            "source_id": "comment-1",
            "collection": "recipe_comment",
        }
    ]


def test_transform_maps_typed_item_to_activity_entry():
    connector = EnzymeConnector(
        api_key="enz_test",
        app_id="nyt-cooking",
        display_name="NYT Cooking Notes",
        content_label="cooking activity",
    )

    @enzyme.transform(connector)
    def cooking_activity(event: CookingEvent) -> Activity:
        return Activity(
            title=event.recipe_name,
            content=event.comment,
            source_id=event.id,
            collections=[event.kind],
            metadata={
                "activity_type": event.kind,
                "labels": [tag for tag in event.auto_tags if tag],
            },
        )

    @enzyme.on_save(connector)
    def save_event(user_id: str, event: CookingEvent) -> CookingEvent:
        return event

    event = CookingEvent(
        id="comment-1",
        kind="recipe_comment",
        recipe_name="Soy-Braised Tofu",
        comment="Good with extra ginger.",
        auto_tags=["weeknight", None, "ginger"],
    )

    connector._connected_users.add("user-123")
    save_event("user-123", event)

    assert connector.collection_for(event) == "recipe_comment"
    assert connector._user_stores["user-123"] == [
        {
            "title": "Soy-Braised Tofu",
            "content": (
                'Good with extra ginger.\n\n'
                'Metadata: {"activity_type": "recipe_comment", '
                '"labels": ["weeknight", "ginger"]}'
            ),
            "source_id": "comment-1",
            "collections": ["recipe_comment"],
        }
    ]


def test_transform_collection_takes_precedence_over_legacy_collection_hook():
    connector = EnzymeConnector(api_key="enz_test", app_id="nyt-cooking")

    @enzyme.collection(connector)
    def legacy_collection(event: CookingEvent) -> str:
        return "legacy"

    @enzyme.transform(connector)
    def cooking_activity(event: CookingEvent) -> dict:
        return {
            "title": event.recipe_name,
            "content": event.comment,
            "collections": ["recipe/main-dishes", "recipe/weeknight"],
        }

    event = CookingEvent(
        id="comment-4",
        kind="recipe_comment",
        recipe_name="Noodles",
        comment="Fast dinner.",
        auto_tags=[],
    )

    assert connector._entry_from_item(event) == {
        "title": "Noodles",
        "content": "Fast dinner.",
        "collections": ["recipe/main-dishes", "recipe/weeknight"],
    }
    assert connector.collection_for(event) == "recipe/main-dishes"


def test_activity_folds_metadata_into_content_string():
    entry = Activity(
        title="Preference",
        content="User prefers ginger in weeknight recipes.",
        collections=["agent/observed-preferences"],
        metadata={"kind": "observed_preference", "signals": ["ginger", "weeknight"]},
    ).to_entry()

    assert entry == {
        "title": "Preference",
        "content": (
            'User prefers ginger in weeknight recipes.\n\n'
            'Metadata: {"kind": "observed_preference", '
            '"signals": ["ginger", "weeknight"]}'
        ),
        "collections": ["agent/observed-preferences"],
    }


def test_hydrate_uses_registered_item_mapping_for_dataclasses():
    connector = EnzymeConnector(api_key="enz_test", app_id="nyt-cooking")

    @enzyme.collection(connector)
    def cooking_collection(event: CookingEvent) -> str:
        return event.kind

    @enzyme.on_save(
        connector,
        title="recipe_name",
        content="comment",
        tags="auto_tags",
        primitive="kind",
        source_id="id",
    )
    def save_event(user_id: str, event: CookingEvent) -> CookingEvent:
        return event

    @enzyme.hydrate(connector)
    def hydrate_events(user_id: str) -> list[CookingEvent]:
        return [
            CookingEvent(
                id="comment-2",
                kind="recipe_comment",
                recipe_name="Miso Soup",
                comment="Reliable weekday version.",
                auto_tags=["weekday"],
            )
        ]

    connector._run_pipeline = lambda user_id, entries: True

    status = connector.connect_user("user-123")

    assert status == {"user_id": "user-123", "entries": 1}
    assert connector._user_stores["user-123"][0]["collection"] == "recipe_comment"
    assert connector._user_stores["user-123"][0]["source_id"] == "comment-2"


def test_collection_hook_can_map_item_to_multiple_collection_labels():
    connector = EnzymeConnector(api_key="enz_test", app_id="nyt-cooking")

    @enzyme.collection(connector)
    def cooking_collections(event: CookingEvent) -> list[str]:
        return ["recipe/main-dishes", "recipe/weeknight", "recipe/main-dishes"]

    @enzyme.on_save(
        connector,
        title="recipe_name",
        content="comment",
        tags="auto_tags",
        primitive="kind",
        source_id="id",
    )
    def save_event(user_id: str, event: CookingEvent) -> CookingEvent:
        return event

    event = CookingEvent(
        id="comment-3",
        kind="recipe_comment",
        recipe_name="Ginger Noodles",
        comment="Fast weeknight dinner.",
        auto_tags=["ginger"],
    )

    connector._connected_users.add("user-123")
    save_event("user-123", event)

    entry = connector._user_stores["user-123"][0]
    assert entry["collections"] == ["recipe/main-dishes", "recipe/weeknight"]
    assert "collection" not in entry
    assert connector.collection_for(event) == "recipe/main-dishes"


def test_connector_hosted_uses_connector_app_scope_without_public_client_import():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "scope": "nyt-cooking/user-123",
                "query": "ginger weeknight dinners",
                "catalysts": [],
                "results": [],
                "total": 0,
            },
        )

    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://search.test/v1/scopes/nyt-cooking/user-123",
    )
    connector = EnzymeConnector(api_key="enz_test", app_id="nyt-cooking")

    with connector.hosted("user-123", base_url="https://search.test", http_client=http_client) as scope:
        response = scope.catalyze("ginger weeknight dinners", limit=3)

    assert seen["path"] == "/v1/scopes/nyt-cooking/user-123/catalyze"
    assert seen["body"] == {
        "query": "ginger weeknight dinners",
        "limit": 3,
        "register": "explore",
        "debug": False,
    }
    assert response.scope == "nyt-cooking/user-123"
