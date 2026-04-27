"""Comprehensive end-to-end test suite for the Enzyme decorator infrastructure.

Covers: decorator registration, deferred init, user connection lifecycle,
search, overview, incremental saves, dev mode, MCP server, DishGen integration,
and full CRUD + Enzyme flow.
"""

from __future__ import annotations

import json
import sys
import os

import pytest

# Ensure the repo root is importable so examples/ can be reached as a package.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from enzyme_sdk.enzyme import EnzymeHosted, EntityConfig, DevSession, enzyme, _Enzyme


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_ENTRIES = [
    {
        "title": "Margherita Pizza",
        "content": "Classic pizza with tomato sauce, mozzarella, and basil.",
        "tags": ["italian", "quick", "vegetarian"],
    },
    {
        "title": "Pad Thai",
        "content": "Stir-fried rice noodles with shrimp, peanuts, and tamarind sauce.",
        "tags": ["thai", "quick", "seafood"],
    },
    {
        "title": "Vegetable Curry",
        "content": "Rich coconut curry with seasonal vegetables and jasmine rice.",
        "tags": ["indian", "vegetarian", "spicy"],
    },
]


def _make_client(**kwargs) -> EnzymeHosted:
    defaults = dict(api_key="enz_test", display_name="Test App")
    defaults.update(kwargs)
    return EnzymeHosted(**defaults)


def _as_list(result) -> list[dict]:
    """Normalise search results — handles both keyword (list) and enzyme (CatalyzeResponse)."""
    if isinstance(result, list):
        return result
    # CatalyzeResponse
    return [{"title": r.file_path, "content": r.content, "tags": [], "_score": r.similarity}
            for r in result.results]


def _as_dict(result) -> dict:
    """Normalise overview — handles both keyword (dict) and enzyme (PetriResponse)."""
    if isinstance(result, dict):
        return result
    return {
        "entry_count": result.total_entities,
        "top_tags": [{"tag": e.name, "count": e.frequency} for e in result.entities],
        "entity_types": {},
    }


def _fresh_decorated_client():
    """Return a fresh EnzymeHosted plus decorated save/hydrate functions backed
    by an in-memory store so each test is isolated."""
    client = _make_client()
    store: dict[str, list[dict]] = {
        "alice": list(SAMPLE_ENTRIES),
        "bob": [SAMPLE_ENTRIES[1]],
    }

    # We need a fresh _Enzyme to avoid cross-test pollution in the singleton,
    # but the decorators only touch the *client*, so re-using the global
    # ``enzyme`` singleton is fine.

    @enzyme.on_save(client, entity="dish", title="title", content="content", tags="tags")
    def save_dish(user_id: str, data: dict) -> dict:
        """Persist a dish entry."""
        store.setdefault(user_id, []).append(data)
        return data  # unchanged return

    @enzyme.hydrate(client, entity="dish")
    def hydrate_dishes(user_id: str) -> list[dict]:
        """Fetch all dishes for a user."""
        return store.get(user_id, [])

    return client, save_dish, hydrate_dishes, store


# ===================================================================
# 1. Decorator Registration
# ===================================================================


class TestDecoratorRegistration:
    def test_no_entities_initially(self):
        client = _make_client()
        assert client._entities == {}
        assert client._save_fns == {}
        assert client._hydrate_fns == {}

    def test_on_save_registers_entity_and_function(self):
        client = _make_client()

        @enzyme.on_save(client, "note")
        def save_note(user_id: str, data: dict) -> dict:
            return data

        assert "note" in client._entities
        assert "note" in client._save_fns
        assert client._save_fns["note"] is save_note

    def test_hydrate_registers_entity_and_function(self):
        client = _make_client()

        @enzyme.hydrate(client, entity="note")
        def hydrate_notes(user_id: str) -> list[dict]:
            return []

        assert "note" in client._entities
        assert "note" in client._hydrate_fns
        assert client._hydrate_fns["note"] is hydrate_notes

    def test_multiple_entities_on_same_client(self):
        client = _make_client()

        @enzyme.on_save(client, "recipe")
        def save_recipe(user_id, data):
            return data

        @enzyme.on_save(client, "bookmark")
        def save_bookmark(user_id, data):
            return data

        assert set(client._entities.keys()) == {"recipe", "bookmark"}

    def test_decorator_preserves_function_name_and_docstring(self):
        client = _make_client()

        @enzyme.on_save(client, "widget")
        def save_widget(user_id: str, data: dict) -> dict:
            """Save a widget for the user."""
            return data

        assert save_widget.__name__ == "save_widget"
        assert save_widget.__doc__ == "Save a widget for the user."

    def test_hydrate_decorator_does_not_wrap_function(self):
        """@enzyme.hydrate returns the original function unwrapped."""
        client = _make_client()

        def original_fn(user_id: str) -> list[dict]:
            """Original docstring."""
            return []

        decorated = enzyme.hydrate(client, entity="thing")(original_fn)
        assert decorated is original_fn


# ===================================================================
# 2. Deferred Init / No Data Until Connection
# ===================================================================


class TestDeferredInit:
    def test_is_connected_false_for_unknown_user(self):
        client = _make_client()
        assert client.is_connected("ghost") is False

    def test_search_raises_for_unconnected_user(self):
        client, *_ = _fresh_decorated_client()
        with pytest.raises(RuntimeError, match="not connected"):
            client.search("unknown_user", "pizza")

    def test_overview_raises_for_unconnected_user(self):
        client, *_ = _fresh_decorated_client()
        with pytest.raises(RuntimeError, match="not connected"):
            client.overview("unknown_user")

    def test_on_save_does_not_queue_for_unconnected_user(self):
        client, save_dish, _, store = _fresh_decorated_client()
        # Call save_dish for an unconnected user
        result = save_dish("stranger", {"title": "Ramen", "content": "Noodles", "tags": []})
        # The function still returns the data unchanged
        assert result["title"] == "Ramen"
        # But the client's internal store should be empty for that user
        assert "stranger" not in client._user_stores


# ===================================================================
# 3. User Connection Lifecycle
# ===================================================================


class TestUserConnectionLifecycle:
    def test_connect_user_calls_fetch_and_populates_store(self):
        client, save_dish, fetch_dishes, store = _fresh_decorated_client()
        client.connect_user("alice")
        assert client.is_connected("alice")
        entries = client._user_stores["alice"]
        # alice has 3 sample entries
        assert len(entries) == 3
        # Each entry should be enriched with 'entity'
        assert all(e.get("entity") == "dish" for e in entries)

    def test_connected_users_property(self):
        client, *_ = _fresh_decorated_client()
        assert client.connected_users == set()
        client.connect_user("alice")
        client.connect_user("bob")
        assert client.connected_users == {"alice", "bob"}

    def test_disconnect_user_removes_from_connected_set(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        assert client.is_connected("alice")
        client.disconnect_user("alice")
        assert not client.is_connected("alice")

    def test_disconnect_retains_data(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        data_before = list(client._user_stores["alice"])
        client.disconnect_user("alice")
        # Data is still in the store
        assert client._user_stores["alice"] == data_before

    def test_reconnect_re_fetches_cleanly(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        count_after_first = len(client._user_stores["alice"])
        # Reconnect — data is replaced, not duplicated
        client.connect_user("alice")
        assert len(client._user_stores["alice"]) == count_after_first


# ===================================================================
# 4. Search (post-connection)
# ===================================================================


class TestSearch:
    def setup_method(self):
        self.client, self.save_dish, self.fetch_dishes, self.store = (
            _fresh_decorated_client()
        )
        self.client.connect_user("alice")

    def test_search_returns_results_sorted_by_relevance(self):
        results = _as_list(self.client.search("alice", "vegetarian"))
        assert len(results) > 0
        scores = [r["_score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_results_have_score_field(self):
        results = _as_list(self.client.search("alice", "pizza"))
        for r in results:
            assert "_score" in r
            assert isinstance(r["_score"], float)

    def test_search_no_matches_returns_empty(self):
        results = _as_list(self.client.search("alice", "xyznonexistent"))
        assert results == []

    def test_search_respects_limit(self):
        results = _as_list(self.client.search("alice", "quick", limit=1))
        assert len(results) <= 1

    def test_search_matches_title(self):
        results = _as_list(self.client.search("alice", "Margherita"))
        assert any("Margherita" in r["title"] for r in results)

    def test_search_matches_content(self):
        results = _as_list(self.client.search("alice", "tamarind"))
        assert len(results) > 0
        assert any("Pad Thai" in r["title"] for r in results)

    def test_search_matches_tags(self):
        results = _as_list(self.client.search("alice", "thai"))
        assert len(results) > 0


# ===================================================================
# 5. Overview
# ===================================================================


class TestOverview:
    def test_overview_returns_expected_keys(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        ov = _as_dict(client.overview("alice"))
        assert "entry_count" in ov
        assert "top_tags" in ov
        assert "entity_types" in ov

    def test_overview_entry_count(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        ov = _as_dict(client.overview("alice"))
        assert ov["entry_count"] == 3

    def test_overview_entity_types(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        ov = _as_dict(client.overview("alice"))
        assert "dish" in ov["entity_types"]

    def test_overview_top_tags(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        ov = _as_dict(client.overview("alice"))
        tag_names = [t["tag"] for t in ov["top_tags"]]
        # "vegetarian" appears in 2 of 3 entries
        assert "vegetarian" in tag_names


# ===================================================================
# 6. Incremental Saves (on_save after connection)
# ===================================================================


class TestIncrementalSaves:
    def test_save_after_connect_queues_entry(self):
        client, save_dish, _, store = _fresh_decorated_client()
        client.connect_user("alice")
        count_before = len(client._user_stores["alice"])
        save_dish("alice", {"title": "New Dish", "content": "Something new", "tags": ["new"]})
        assert len(client._user_stores["alice"]) == count_before + 1

    def test_saved_entry_is_searchable_immediately(self):
        client, save_dish, _, _ = _fresh_decorated_client()
        client.connect_user("alice")
        save_dish("alice", {
            "title": "Szechuan Tofu",
            "content": "Fiery Szechuan peppercorn tofu stir-fry.",
            "tags": ["chinese", "spicy"],
        })
        results = _as_list(client.search("alice", "Szechuan"))
        assert len(results) > 0
        assert any("Szechuan" in r["title"] for r in results)

    def test_multiple_saves_accumulate(self):
        client, save_dish, _, _ = _fresh_decorated_client()
        client.connect_user("alice")
        count_before = len(client._user_stores["alice"])
        for i in range(5):
            save_dish("alice", {"title": f"Dish {i}", "content": f"Content {i}", "tags": []})
        assert len(client._user_stores["alice"]) == count_before + 5


# ===================================================================
# 7. Dev Mode
# ===================================================================


class TestDevMode:
    def test_dev_returns_dev_session(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        assert isinstance(session, DevSession)

    def test_dev_search_works_without_connection(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        results = session.search("pizza")
        assert len(results) > 0

    def test_dev_overview(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        ov = session.overview()
        assert ov["entry_count"] == len(SAMPLE_ENTRIES)
        assert "top_tags" in ov

    def test_dev_status(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        status = session.status()
        assert status["entity"] == "recipe"
        assert status["entry_count"] == len(SAMPLE_ENTRIES)
        assert status["mode"] == "dev"


# ===================================================================
# 8. MCP Server (as_mcp_app)
# ===================================================================


class TestMCPServer:
    """Test the FastAPI MCP server returned by ``as_mcp_app``."""

    def _build_mcp_client(self):
        """Create a client + MCP app and return an (EnzymeHosted, app) pair."""
        client, save_dish, hydrate_dishes, store = _fresh_decorated_client()
        client.connect_user("alice")
        mcp_app = client.as_mcp_app(whitelist=["alice"])
        return client, mcp_app, save_dish

    def _rpc(self, method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params or {},
        }

    @pytest.mark.anyio
    async def test_health_returns_200(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    @pytest.mark.anyio
    async def test_mcp_no_auth_returns_401(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/mcp", json=self._rpc("tools/list"))
            assert resp.status_code == 401
            assert "WWW-Authenticate" in resp.headers

    @pytest.mark.anyio
    async def test_tools_list_matches_registered_entities(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/mcp",
                json=self._rpc("tools/list"),
                headers={"Authorization": "Bearer test-token", "X-Enzyme-User": "alice"},
            )
            assert resp.status_code == 200
            body = resp.json()
            tools = body["result"]["tools"]
            tool_names = {t["name"] for t in tools}
            # "dish" entity -> plural "dishes" -> search_dishs, get_dish_profile
            assert "search_dishs" in tool_names
            assert "get_dish_profile" in tool_names

    @pytest.mark.anyio
    async def test_tools_call_search_returns_results(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/mcp",
                json=self._rpc("tools/call", {
                    "name": "search_dishs",
                    "arguments": {"query": "pizza"},
                }),
                headers={"Authorization": "Bearer test-token", "X-Enzyme-User": "alice"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "result" in body
            content = body["result"]["content"]
            assert len(content) > 0
            text = content[0]["text"]
            # Response may be JSON (keyword fallback) or markdown (enzyme pipeline)
            try:
                parsed = json.loads(text)
                assert isinstance(parsed, list)
                assert len(parsed) > 0
            except json.JSONDecodeError:
                # render_to_prompt returns a markdown string
                assert "pizza" in text.lower() or "search" in text.lower()

    @pytest.mark.anyio
    async def test_tools_call_unknown_tool_returns_error(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/mcp",
                json=self._rpc("tools/call", {
                    "name": "nonexistent_tool",
                    "arguments": {},
                }),
                headers={"Authorization": "Bearer test-token", "X-Enzyme-User": "alice"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "error" in body
            assert body["error"]["code"] == -32601

    @pytest.mark.anyio
    async def test_unknown_method_returns_error(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/mcp",
                json=self._rpc("bogus/method"),
                headers={"Authorization": "Bearer test-token", "X-Enzyme-User": "alice"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert "error" in body
            assert body["error"]["code"] == -32601


# ===================================================================
# 9. DishGen Integration Test
# ===================================================================


class TestDishGenIntegration:
    """Integration tests that import the actual DishGen example app."""

    def _import_dishgen(self):
        """Import dishgen_app, returning (app_enzyme, save_recipe, fetch_recipes, db).

        Each call returns a reference to the module-level objects — the db is
        pre-seeded but the client may carry state from prior tests, so we
        disconnect all users first.
        """
        from examples.dishgen_app import app_enzyme, save_recipe, hydrate_recipes, db

        # Clean slate for connection state
        for uid in list(app_enzyme._connected_users):
            app_enzyme.disconnect_user(uid)
        app_enzyme._user_stores.clear()
        return app_enzyme, save_recipe, hydrate_recipes, db

    def test_entities_registered(self):
        app_enzyme, *_ = self._import_dishgen()
        assert "recipe" in app_enzyme._entities
        assert "recipe" in app_enzyme._save_fns
        assert "recipe" in app_enzyme._hydrate_fns

    def test_connect_christa_and_search_eggplant(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        results = _as_list(app_enzyme.search("christa", "eggplant"))
        assert len(results) > 0

    def test_connect_es_and_search_chicken(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("es")
        results = _as_list(app_enzyme.search("es", "chicken"))
        assert len(results) > 0

    def test_new_recipe_via_save_is_searchable(self):
        app_enzyme, save_recipe, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        save_recipe("christa", {
            "title": "Avocado Toast Deluxe",
            "instructions": "Toast sourdough, smash avocado with chili flakes and lime.",
            "tags": ["breakfast", "quick", "vegetarian"],
        })
        results = _as_list(app_enzyme.search("christa", "avocado toast"))
        assert len(results) > 0
        assert any("Avocado" in r["title"] for r in results)

    def test_overview_entry_count(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        ov = _as_dict(app_enzyme.overview("christa"))
        assert ov["entry_count"] > 0


# ===================================================================
# 10. Full CRUD + Enzyme Flow (via httpx TestClient on DishGen FastAPI)
# ===================================================================


class TestFullCRUDFlow:
    """End-to-end: HTTP CRUD on the DishGen FastAPI app + Enzyme indexing."""

    def _setup(self):
        from examples.dishgen_app import app, app_enzyme, save_recipe, db

        # Reset enzyme connection state
        for uid in list(app_enzyme._connected_users):
            app_enzyme.disconnect_user(uid)
        app_enzyme._user_stores.clear()
        return app, app_enzyme, save_recipe, db

    @pytest.mark.anyio
    async def test_create_recipe_returns_201(self):
        import httpx

        app, app_enzyme, _, db = self._setup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post("/users/alice/recipes", json={
                "title": "Grilled Halloumi Salad",
                "instructions": "Grill halloumi slices and serve on arugula with pomegranate.",
                "tags": ["vegetarian", "mediterranean", "quick"],
                "rating": 4,
            })
            assert resp.status_code == 201
            data = resp.json()
            assert data["title"] == "Grilled Halloumi Salad"
            assert "id" in data

    @pytest.mark.anyio
    async def test_list_recipes_includes_new_recipe(self):
        import httpx

        app, app_enzyme, _, db = self._setup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            # Create a new recipe
            create_resp = await ac.post("/users/alice/recipes", json={
                "title": "Crispy Chickpea Bowl",
                "instructions": "Roast chickpeas with spices, serve over rice.",
                "tags": ["vegetarian"],
            })
            assert create_resp.status_code == 201
            new_id = create_resp.json()["id"]

            # List recipes
            list_resp = await ac.get("/users/alice/recipes")
            assert list_resp.status_code == 200
            recipes = list_resp.json()
            assert any(r["id"] == new_id for r in recipes)

    @pytest.mark.anyio
    async def test_connect_user_then_search_finds_hydrated_data(self):
        import httpx

        app, app_enzyme, _, db = self._setup()
        # Connect christa — hydrate pulls her ~325 NYT recipes
        app_enzyme.connect_user("christa")

        results = _as_list(app_enzyme.search("christa", "eggplant"))
        assert len(results) > 0

    @pytest.mark.anyio
    async def test_update_recipe_notifies_enzyme(self):
        import httpx

        app, app_enzyme, _, db = self._setup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            # Connect christa first so on_save will queue
            app_enzyme.connect_user("christa")
            count_before = len(app_enzyme._user_stores.get("christa", []))

            # Update an existing NYT recipe (christa has "nyt-0")
            resp = await ac.patch("/users/christa/recipes/nyt-0", json={
                "title": "Beet Noodles (Updated)",
                "tags": ["vegetarian", "umami"],
            })
            assert resp.status_code == 200

            # on_save should have queued the updated entry
            count_after = len(app_enzyme._user_stores.get("christa", []))
            assert count_after > count_before

    @pytest.mark.anyio
    async def test_delete_recipe_returns_204(self):
        import httpx

        app, app_enzyme, _, db = self._setup()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            # Create then delete
            create_resp = await ac.post("/users/alice/recipes", json={
                "title": "Temporary Recipe",
                "instructions": "This will be deleted.",
                "tags": ["temp"],
            })
            recipe_id = create_resp.json()["id"]

            del_resp = await ac.delete(f"/users/alice/recipes/{recipe_id}")
            assert del_resp.status_code == 204

            # Verify it is gone from the DB
            get_resp = await ac.get(f"/users/alice/recipes/{recipe_id}")
            assert get_resp.status_code == 404
