"""Comprehensive end-to-end test suite for the Enzyme decorator infrastructure.

Covers: decorator registration, deferred init, user connection lifecycle,
search, overview, incremental saves, dev mode, MCP server, DishGen integration,
and full CRUD + Enzyme flow.

The test suite builds one canonical enzyme vault (ingest + init) at session
start. Each test that needs a vault clones it, so connect_user finds an
existing index and refreshes instantly instead of re-running init.
"""

from __future__ import annotations

import json
import shutil
import sys
import os
from pathlib import Path

import pytest

# Ensure the repo root is importable so examples/ can be reached as a package.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from enzyme_sdk.enzyme import EnzymeConnector, CorpusConfig, DevSession, enzyme, _Enzyme


# ---------------------------------------------------------------------------
# Shared test data
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

# ---------------------------------------------------------------------------
# Canonical vault — built once per session, cloned per test
# ---------------------------------------------------------------------------

_CANONICAL_COLLECTION = "_test-canonical"


@pytest.fixture(scope="session", autouse=True)
def _build_canonical_vault():
    """Ingest + init the sample entries once. Subsequent tests clone this vault."""
    from enzyme_sdk.client import EnzymeClient
    from enzyme_sdk.store import VaultStore

    # Ensure ENZYME_HOME is isolated
    EnzymeConnector._ensure_enzyme_home()

    ec = EnzymeClient()
    store = VaultStore()
    vault_path = store.vault_path(_CANONICAL_COLLECTION)

    # Check if already built (persists across runs)
    try:
        st = ec.status(vault=str(vault_path))
        if st.catalysts > 0 and st.documents == len(SAMPLE_ENTRIES):
            return  # already good
    except Exception:
        pass

    # Build from scratch: cluster → ingest → init
    if vault_path.exists():
        shutil.rmtree(vault_path)
    store.create_vault(_CANONICAL_COLLECTION)

    # Auto-cluster to assign tags (same as _run_pipeline does)
    entries = list(SAMPLE_ENTRIES)
    assigned = ec.cluster_entries(
        entries,
        text=lambda e: f"{e.get('title', '')}\n\n{e.get('content', '')}",
    )
    ec.ingest(vault=str(vault_path), entries=assigned.entries)
    ec.init(vault=str(vault_path), quiet=True)


def _clone_canonical_vault(collection_id: str):
    """Copy the canonical vault's .enzyme dir into a target collection."""
    from enzyme_sdk.store import VaultStore
    store = VaultStore()
    src = store.vault_path(_CANONICAL_COLLECTION) / ".enzyme"
    dst_vault = store.vault_path(collection_id)
    dst_vault.mkdir(parents=True, exist_ok=True)
    dst_enzyme = dst_vault / ".enzyme"
    if dst_enzyme.exists():
        shutil.rmtree(dst_enzyme)
    shutil.copytree(src, dst_enzyme)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_client(**kwargs) -> EnzymeConnector:
    defaults = dict(api_key="enz_test", display_name="Test App", content_label="dishes")
    defaults.update(kwargs)
    return EnzymeConnector(**defaults)






def _fresh_decorated_client():
    """Return a fresh EnzymeConnector plus decorated save/hydrate functions backed
    by an in-memory store. The underlying vault is cloned from the canonical
    build so connect_user hits a pre-indexed vault."""
    client = _make_client()
    store: dict[str, list[dict]] = {
        "alice": list(SAMPLE_ENTRIES),
        "bob": [SAMPLE_ENTRIES[1]],
    }

    # Clone canonical vault for this client's users
    for uid in store:
        _clone_canonical_vault(client._user_collection_id(uid))

    @enzyme.on_save(client, title="title", content="content", tags="tags")
    def save_dish(user_id: str, data: dict) -> dict:
        """Persist a dish entry."""
        store.setdefault(user_id, []).append(data)
        return data  # unchanged return

    @enzyme.hydrate(client)
    def hydrate_dishes(user_id: str) -> list[dict]:
        """Fetch all dishes for a user."""
        return store.get(user_id, [])

    return client, save_dish, hydrate_dishes, store


# ===================================================================
# 1. Decorator Registration
# ===================================================================


class TestDecoratorRegistration:
    def test_default_corpus_initially(self):
        client = _make_client()
        assert list(client._corpora.keys()) == [client._default_corpus]
        assert client._save_fns == {}
        assert client._hydrate_fns == {}

    def test_on_save_registers_function(self):
        client = _make_client()

        @enzyme.on_save(client)
        def save_note(user_id: str, data: dict) -> dict:
            return data

        assert client._default_corpus in client._save_fns
        assert client._save_fns[client._default_corpus] is save_note

    def test_hydrate_registers_function(self):
        client = _make_client()

        @enzyme.hydrate(client)
        def hydrate_notes(user_id: str) -> list[dict]:
            return []

        assert client._default_corpus in client._hydrate_fns
        assert client._hydrate_fns[client._default_corpus] is hydrate_notes

    def test_tool_metadata_lives_on_client(self):
        client = _make_client()
        cfg = client._corpora[client._default_corpus]
        assert cfg.catalyze_tool_name == "catalyze_dishes"
        assert cfg.profile_tool_name == "get_dishes_profile"

    def test_decorator_preserves_function_name_and_docstring(self):
        client = _make_client()

        @enzyme.on_save(client)
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

        decorated = enzyme.hydrate(client)(original_fn)
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
        # Entries are stored unchanged; app-level content type is not injected.
        assert all("entity" not in e for e in entries)

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

    def test_search_returns_catalyze_response(self):
        resp = self.client.search("alice", "vegetarian")
        assert hasattr(resp, "results")
        assert len(resp.results) > 0
        # Results sorted by similarity descending
        sims = [r.similarity for r in resp.results]
        assert sims == sorted(sims, reverse=True)

    def test_search_results_have_similarity(self):
        resp = self.client.search("alice", "pizza")
        for r in resp.results:
            assert isinstance(r.similarity, float)

    def test_search_respects_limit(self):
        resp = self.client.search("alice", "quick", limit=1)
        assert len(resp.results) <= 1

    def test_search_finds_margherita(self):
        resp = self.client.search("alice", "Margherita pizza")
        assert any("margherita" in r.file_path.lower() for r in resp.results)

    def test_search_finds_by_content(self):
        resp = self.client.search("alice", "tamarind noodles")
        assert len(resp.results) > 0

    def test_search_has_catalysts(self):
        resp = self.client.search("alice", "vegetarian comfort")
        assert len(resp.top_contributing_catalysts) > 0

    def test_search_renders_to_prompt(self):
        resp = self.client.search("alice", "thai")
        prompt = resp.render_to_prompt()
        assert "Enzyme search" in prompt


# ===================================================================
# 5. Overview
# ===================================================================


class TestOverview:
    def test_overview_returns_petri_response(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        resp = client.overview("alice")
        assert hasattr(resp, "entities")
        assert resp.total_entities > 0

    def test_overview_has_entities_with_catalysts(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        resp = client.overview("alice")
        assert len(resp.entities) > 0
        # Each entity should have catalysts
        assert any(len(e.catalysts) > 0 for e in resp.entities)

    def test_overview_renders_to_prompt(self):
        client, *_ = _fresh_decorated_client()
        client.connect_user("alice")
        resp = client.overview("alice")
        prompt = resp.render_to_prompt()
        assert "Enzyme context" in prompt


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

    def test_saved_entry_is_in_store_immediately(self):
        client, save_dish, _, _ = _fresh_decorated_client()
        client.connect_user("alice")
        save_dish("alice", {
            "title": "Szechuan Tofu",
            "content": "Fiery Szechuan peppercorn tofu stir-fry.",
            "tags": ["chinese", "spicy"],
        })
        # Entry is in the in-memory store immediately (searchable via keyword).
        # The enzyme vault catches up on the next refresh (debounced).
        entries = client._user_stores["alice"]
        assert any("Szechuan" in e.get("title", "") for e in entries)

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

    def test_dev_search_returns_catalyze_response(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        resp = session.search("pizza")
        assert hasattr(resp, "results")
        assert len(resp.results) > 0

    def test_dev_overview_returns_petri_response(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        resp = session.overview()
        assert hasattr(resp, "entities")
        assert resp.total_entities > 0

    def test_dev_status(self):
        client = _make_client()
        session = client.dev("recipe", SAMPLE_ENTRIES)
        status = session.status()
        assert status["entity"] == "recipe"
        assert status["documents"] == len(SAMPLE_ENTRIES)
        assert status["mode"] == "dev"


# ===================================================================
# 8. MCP Server (as_mcp_app)
# ===================================================================


class TestMCPServer:
    """Test the FastAPI MCP server returned by ``as_mcp_app``."""

    def _build_mcp_client(self):
        """Create a client + MCP app and return an (EnzymeConnector, app) pair."""
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
    async def test_tools_list_matches_registered_corpora(self):
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
            assert "catalyze_dishes" in tool_names
            assert "get_dishes_profile" in tool_names

    @pytest.mark.anyio
    async def test_tools_call_search_returns_results(self):
        import httpx

        _, mcp_app, _ = self._build_mcp_client()
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/mcp",
                json=self._rpc("tools/call", {
                    "name": "catalyze_dishes",
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
            # render_to_prompt returns a markdown string with search results
            assert "Enzyme search" in text or len(text) > 0

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

    def test_corpus_hooks_registered(self):
        app_enzyme, *_ = self._import_dishgen()
        assert app_enzyme._default_corpus in app_enzyme._corpora
        assert app_enzyme._default_corpus in app_enzyme._save_fns
        assert app_enzyme._default_corpus in app_enzyme._hydrate_fns

    def test_connect_christa_and_search_eggplant(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        resp = app_enzyme.search("christa", "eggplant")
        assert len(resp.results) > 0

    def test_connect_es_and_search_chicken(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("es")
        resp = app_enzyme.search("es", "chicken")
        assert len(resp.results) > 0

    def test_new_recipe_via_save_queued_in_store(self):
        app_enzyme, save_recipe, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        save_recipe("christa", {
            "title": "Avocado Toast Deluxe",
            "instructions": "Toast sourdough, smash avocado with chili flakes and lime.",
            "tags": ["breakfast", "quick", "vegetarian"],
        })
        # Entry is queued in the in-memory store immediately.
        # Enzyme vault catches up on next refresh.
        entries = app_enzyme._user_stores["christa"]
        assert any("Avocado" in e.get("title", "") for e in entries)

    def test_overview_has_entities(self):
        app_enzyme, _, _, _ = self._import_dishgen()
        app_enzyme.connect_user("christa")
        resp = app_enzyme.overview("christa")
        assert resp.total_entities > 0


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

        resp = app_enzyme.search("christa", "eggplant")
        assert len(resp.results) > 0

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
