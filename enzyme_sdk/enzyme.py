"""Decorator-based connector integration for Enzyme.

Decorate your existing save/fetch functions. Enzyme turns them into a Claude
connector — with user isolation and catalyst-based personalization.
No data moves until the user opts in.

Quick start::

    export ENZYME_API_KEY=enz_...   # from enzyme.garden/settings

    from enzyme_sdk import EnzymeConnector, enzyme

    client = EnzymeConnector(display_name="DishGen")

    @enzyme.hydrate(client)
    def get_recipes(user_id: str) -> list[dict]:
        return db.get_recipes(owner=user_id)

    @enzyme.on_save(client, title="title", content="instructions", tags="tags")
    def create_recipe(user_id, data):
        return db.insert(data)          # return value unchanged

    client.connect_user("user-42")      # triggers hydrate + index
    client.search("user-42", "comfort food")
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable

from enzyme_sdk.activity import (
    Activity,
    ActivityCollection,
    CatalystProfile,
    collection_id,
)

log = logging.getLogger("enzyme.connector")


def _tool_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return slug or "content"




# ---------------------------------------------------------------------------
# Field extraction helpers for @enzyme.on_save
# ---------------------------------------------------------------------------

def _extract_enzyme_entry(
    result: Any,
    field_map: dict[str, str | Callable],
) -> dict[str, Any]:
    """Pull enzyme fields from an arbitrary return value using a field map.

    Each key in *field_map* is a target enzyme field (title, content, tags …).
    Each value is either:
      - a string  → used as dict key / attribute name on *result*
      - a callable → called with *result*, must return the value
    """
    entry: dict[str, Any] = {}
    for enzyme_field, accessor in field_map.items():
        if callable(accessor):
            entry[enzyme_field] = accessor(result)
        elif isinstance(result, dict):
            entry[enzyme_field] = result.get(accessor, "" if enzyme_field != "tags" else [])
        else:
            entry[enzyme_field] = getattr(result, accessor, "" if enzyme_field != "tags" else [])
    return entry


def _item_as_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, Activity):
        return item.to_entry()
    if isinstance(item, dict):
        return dict(item)
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    if hasattr(item, "dict") and callable(item.dict):
        return item.dict()
    return dict(vars(item))


def _clean_tags(tags: Any) -> list[str]:
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    return [str(tag) for tag in tags if tag not in (None, "")]


def _sanitize_collection(value: Any) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_./-]+", "-", str(value).strip().lower()).strip("-")
    return slug or "content"


def _collection_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [value]
    else:
        try:
            raw_values = list(value)
        except TypeError:
            raw_values = [value]

    values: list[str] = []
    for raw in raw_values:
        cleaned = _sanitize_collection(raw)
        if cleaned and cleaned not in values:
            values.append(cleaned)
    return values


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_entity_ref(entity_ref: str, profile: str | None = None) -> str:
    if profile:
        return (
            "{ "
            f"{_toml_string(entity_ref)} = "
            f"{{ profile = {_toml_string(profile)} }}"
            " }"
        )
    return _toml_string(entity_ref)


def _toml_vault_section(
    vault_key: str,
    collection_ids: list[str],
    profile_by_collection: dict[str, str] | None = None,
) -> str:
    profiles = profile_by_collection or {}
    refs = ", ".join(
        _toml_entity_ref(f"folder:{collection_id}", profiles.get(collection_id))
        for collection_id in collection_ids
    )
    return (
        f"[vaults.{_toml_string(vault_key)}]\n"
        f"entities = [{refs}]\n"
    )


def _replace_toml_vault_section(existing: str, vault_key: str, section: str) -> str:
    header = f"[vaults.{_toml_string(vault_key)}]"
    lines = existing.splitlines()
    output: list[str] = []
    index = 0
    replaced = False

    while index < len(lines):
        if lines[index].strip() == header:
            if output and output[-1].strip():
                output.append("")
            output.extend(section.rstrip().splitlines())
            replaced = True
            index += 1
            while index < len(lines) and not (
                lines[index].startswith("[") and lines[index].strip().endswith("]")
            ):
                index += 1
            continue
        output.append(lines[index])
        index += 1

    if not replaced:
        if output and output[-1].strip():
            output.append("")
        output.extend(section.rstrip().splitlines())

    return "\n".join(output).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CorpusConfig
# ---------------------------------------------------------------------------

@dataclass
class CorpusConfig:
    name: str = "content"
    plural: str = ""
    catalyze_tool_name: str = ""
    profile_tool_name: str = ""
    catalyze_description: str = ""
    profile_description: str = ""

    def __post_init__(self):
        if not self.plural:
            if self.name.endswith("s"):
                self.plural = self.name + "es"
            elif self.name.endswith("y"):
                self.plural = self.name[:-1] + "ies"
            else:
                self.plural = self.name + "s"
        if not self.catalyze_tool_name:
            self.catalyze_tool_name = f"catalyze_{_tool_slug(self.plural)}"
        if not self.profile_tool_name:
            self.profile_tool_name = f"get_{_tool_slug(self.name)}_profile"


# ---------------------------------------------------------------------------
# DevSession
# ---------------------------------------------------------------------------

class DevSession:
    """Returned by ``EnzymeConnector.dev()`` — explore inline data with no setup.

    Creates a temporary enzyme vault, ingests the entries, and runs the full
    pipeline so search and overview work immediately.
    """

    def __init__(self, entity: str, entries: list[dict[str, Any]]) -> None:
        import hashlib
        from enzyme_sdk.client import EnzymeClient
        from enzyme_sdk.store import VaultStore

        self.entity = entity
        self._entries = entries
        self._ec = EnzymeClient()
        self._store = VaultStore()

        # Deterministic collection ID so repeated calls reuse the vault
        content_hash = hashlib.md5(str(entries).encode()).hexdigest()[:8]
        self._collection_id = f"_dev-{entity}-{content_hash}"
        vault_path = self._store.vault_path(self._collection_id)

        # Build vault if needed
        try:
            st = self._ec.status(vault=str(vault_path))
            if st.catalysts > 0 and st.documents == len(entries):
                self._vault = str(vault_path)
                return
        except Exception:
            pass

        self._store.create_vault(self._collection_id)
        self._vault = str(vault_path)
        self._ec.ingest(vault=self._vault, entries=entries)
        self._ec.init(vault=self._vault, quiet=True)

    def search(self, query: str, limit: int = 10) -> Any:
        return self._ec.catalyze(query, vault=self._vault, limit=limit)

    def overview(self, top: int = 10) -> Any:
        return self._ec.petri(vault=self._vault, top=top)

    def status(self) -> dict[str, Any]:
        st = self._ec.status(vault=self._vault)
        return {
            "entity": self.entity,
            "documents": st.documents,
            "catalysts": st.catalysts,
            "entities": st.entities,
            "mode": "dev",
        }

    def __repr__(self) -> str:
        return f"<DevSession entity={self.entity!r} entries={len(self._entries)}>"


# ---------------------------------------------------------------------------
# EnzymeConnector
# ---------------------------------------------------------------------------

class EnzymeConnector:
    """Connect your app data to Enzyme's MCP-facing integration layer.

    Get your API key:
        1. enzyme.garden/login  (GitHub or Google)
        2. enzyme.garden/settings → Create API key
        3. ``export ENZYME_API_KEY=enz_...`` or pass ``api_key=`` directly
    """

    def __init__(
        self,
        api_key: str | None = None,
        app_id: str = "",
        display_name: str = "",
        description: str = "",
        logo_url: str = "",
        system_prompt: str = "",
        content_label: str = "content",
        catalyze_tool: str = "",
        profile_tool: str = "",
        catalyze_description: str = "",
        profile_description: str = "",
        collections: list[ActivityCollection] | None = None,
        catalyst_profiles: dict[ActivityCollection, CatalystProfile] | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ENZYME_API_KEY", "")
        self.app_id = app_id or _tool_slug(display_name or content_label)
        self.display_name = display_name
        self.description = description
        self.logo_url = logo_url
        self.system_prompt = system_prompt
        self.content_label = content_label

        # Registry
        self._corpora: dict[str, CorpusConfig] = {}
        self._save_fns: dict[str, Callable] = {}
        self._hydrate_fns: dict[str, Callable] = {}
        self._transform_fns: dict[str, Callable] = {}
        self._collection_fns: dict[str, Callable] = {}
        self._activity_collections = {
            collection_id(collection)
            for collection in (collections or [])
        }
        self._catalyst_profiles = {
            collection_id(collection): profile.value
            for collection, profile in (catalyst_profiles or {}).items()
        }
        self._field_maps: dict[str, dict[str, str | Callable]] = {}
        self._default_corpus = "_default"
        self._ensure_corpus(
            self._default_corpus,
            name=content_label,
            plural=content_label,
            catalyze_tool_name=catalyze_tool,
            profile_tool_name=profile_tool,
            catalyze_description=catalyze_description,
            profile_description=profile_description,
        )

        # Per-user state
        self._connected_users: set[str] = set()
        self._user_stores: dict[str, list[dict[str, Any]]] = {}

        # Enzyme pipeline (lazy init)
        self._enzyme_client: Any = None
        self._store: Any = None
        self._collections_base: Path | None = None

        # Isolate SDK dev environment from user's ~/.enzyme
        self._ensure_enzyme_home()

    # -- enzyme pipeline (lazy) --------------------------------------------

    @staticmethod
    def _ensure_enzyme_home():
        """Set ENZYME_HOME so SDK dev vaults don't pollute ~/.enzyme/config.toml."""
        if "ENZYME_HOME" not in os.environ:
            sdk_home = Path.home() / ".enzyme-sdk" / ".enzyme"
            sdk_home.mkdir(parents=True, exist_ok=True)
            os.environ["ENZYME_HOME"] = str(sdk_home)

    def _get_enzyme_client(self):
        if self._enzyme_client is None:
            from enzyme_sdk.client import EnzymeClient
            self._enzyme_client = EnzymeClient()
        return self._enzyme_client

    def _get_store(self):
        if self._store is None:
            from enzyme_sdk.store import VaultStore
            self._store = VaultStore()
            self._collections_base = Path(self._store.base_path)
        return self._store

    # -- registration (called by decorators) --------------------------------

    def _register_save(self, fn: Callable, field_map: dict[str, str | Callable]) -> None:
        corpus = self._default_corpus
        self._ensure_corpus(corpus)
        self._save_fns[corpus] = fn
        self._field_maps[corpus] = field_map

    def _register_hydrate(self, fn: Callable) -> None:
        corpus = self._default_corpus
        self._ensure_corpus(corpus)
        self._hydrate_fns[corpus] = fn

    def _register_transform(self, fn: Callable) -> None:
        corpus = self._default_corpus
        self._ensure_corpus(corpus)
        self._transform_fns[corpus] = fn

    def _register_collection(self, fn: Callable) -> None:
        corpus = self._default_corpus
        self._ensure_corpus(corpus)
        self._collection_fns[corpus] = fn

    def _ensure_corpus(self, corpus: str, **kwargs: Any) -> None:
        if corpus not in self._corpora:
            self._corpora[corpus] = CorpusConfig(**({"name": corpus} | kwargs))
        elif kwargs:
            # Update existing config with any new overrides
            cfg = self._corpora[corpus]
            for k, v in kwargs.items():
                if v:
                    setattr(cfg, k, v)

    # -- ingest -------------------------------------------------------------

    def _queue_ingest(self, user_id: str, entry: dict[str, Any]) -> None:
        if user_id not in self._connected_users:
            return
        self._user_stores.setdefault(user_id, []).append(dict(entry))

        # Ingest into the enzyme DB (fast, no embedding yet — refresh debounces)
        try:
            collection_id = self._user_collection_id(user_id)
            store = self._get_store()
            if store.vault_exists(collection_id):
                vault_path = str(store.vault_path(collection_id))
                self._get_enzyme_client().ingest(vault=vault_path, entry=entry)
        except Exception as exc:
            log.debug("incremental ingest failed: %s", exc)

    def _entry_from_item(self, item: Any, corpus: str | None = None) -> dict[str, Any]:
        corpus = corpus or self._default_corpus
        transform_fn = self._transform_fns.get(corpus)

        if transform_fn:
            entry = _item_as_dict(transform_fn(item))
        elif (field_map := self._field_maps.get(corpus)):
            if "_map" in field_map:
                entry = dict(field_map["_map"](item))
            else:
                entry = _extract_enzyme_entry(item, field_map)
        else:
            entry = _item_as_dict(item)

        collection_fn = self._collection_fns.get(corpus)
        if collection_fn and "collection" not in entry and "collections" not in entry:
            collections = _collection_values(collection_fn(item))
            if len(collections) == 1:
                entry["collection"] = collections[0]
            elif collections:
                entry["collections"] = collections

        if "collection" in entry:
            collections = _collection_values(entry.get("collection"))
            if collections:
                entry["collection"] = collections[0]
            else:
                entry.pop("collection", None)

        if "collections" in entry:
            collections = _collection_values(entry.get("collections"))
            if collections:
                entry["collections"] = collections
                entry.pop("collection", None)
            else:
                entry.pop("collections", None)

        if "tags" in entry:
            entry["tags"] = _clean_tags(entry.get("tags"))

        if self._activity_collections:
            unknown = [
                collection
                for collection in entry.get("collections", [])
                if collection not in self._activity_collections
            ]
            if unknown:
                raise ValueError(
                    "Unknown Activity collection(s): "
                    + ", ".join(sorted(set(unknown)))
                )

        return entry

    def collection_for(self, item: Any, corpus: str | None = None) -> str:
        """Return the connector collection id for a typed source item."""
        corpus = corpus or self._default_corpus
        transform_fn = self._transform_fns.get(corpus)
        if transform_fn:
            entry = _item_as_dict(transform_fn(item))
            collections = _collection_values(
                entry.get("collections") or entry.get("collection")
            )
            if collections:
                return collections[0]
        collection_fn = self._collection_fns.get(corpus)
        if collection_fn:
            collections = _collection_values(collection_fn(item))
            if collections:
                return collections[0]
        return _sanitize_collection(self._corpora.get(corpus, CorpusConfig()).name)

    # -- user lifecycle -----------------------------------------------------

    def _user_collection_id(self, user_id: str) -> str:
        slug = self.display_name.lower().replace(" ", "-") or "enzyme"
        return f"{slug}--{user_id}"

    def connect_user(self, user_id: str) -> dict[str, Any]:
        """Connect a user — calls ``@hydrate`` functions and indexes their data.

        In production this is triggered by OAuth. In dev/test, call directly.
        Re-connecting replaces the user's data with a fresh fetch.

        Returns a status dict with entry count and whether the full pipeline ran.
        """
        self._connected_users.add(user_id)
        store: list[dict[str, Any]] = []
        self._user_stores[user_id] = store

        for corpus, hydrate_fn in self._hydrate_fns.items():
            entries = hydrate_fn(user_id)
            if entries:
                for entry in entries:
                    store.append(self._entry_from_item(entry, corpus))

        # Run enzyme pipeline: ingest → init/refresh
        if store:
            log.info("indexing %s (%d entries)…", user_id, len(store))
            self._run_pipeline(user_id, store)
            log.info("indexed %s ✓", user_id)

        return {"user_id": user_id, "entries": len(store)}

    def _run_pipeline(self, user_id: str, entries: list[dict[str, Any]]) -> bool:
        """Run cluster → ingest → init/refresh for a user. Returns True on success."""
        ec = self._get_enzyme_client()
        store = self._get_store()
        collection_id = self._user_collection_id(user_id)

        # Create vault directory if needed
        if not store.vault_exists(collection_id):
            store.create_vault(collection_id)

        vault_path = str(store.vault_path(collection_id))

        clean_entries = list(entries)

        # Auto-cluster entries to assign tags (unsupervised label discovery).
        # Tags become entities in the enzyme index, which drive catalyst generation.
        if len(clean_entries) >= 3:
            try:
                assigned = ec.cluster_entries(
                    clean_entries,
                    text=lambda e: f"{e.get('title', '')}\n\n{e.get('content', '')}",
                )
                clean_entries = assigned.entries
                log.info("clustered %d entries → %d clusters",
                         len(clean_entries), len(assigned.clusters))
            except Exception as exc:
                log.warning("clustering failed, ingesting without tags: %s", exc)

        # Ingest into the enzyme DB
        ec.ingest(vault=vault_path, entries=clean_entries)
        self._write_collection_entities_config(Path(vault_path), clean_entries)

        # If the vault already has catalysts, refresh (fast — only processes new entries).
        # Otherwise, run full init (embed + select entities + generate catalysts).
        try:
            st = ec.status(vault=vault_path)
            if st.catalysts > 0:
                log.info("refreshing %s (existing index: %d catalysts)", user_id, st.catalysts)
                ec.refresh(vault=vault_path, quiet=True)
            else:
                ec.init(vault=vault_path, quiet=True)
        except Exception:
            ec.init(vault=vault_path, quiet=True)
        return True

    def _write_collection_entities_config(
        self,
        vault_path: Path,
        entries: list[dict[str, Any]],
    ) -> None:
        collections: list[str] = []
        for entry in entries:
            collections.extend(_collection_values(entry.get("collections")))
            collections.extend(_collection_values(entry.get("collection")))

        collection_ids = list(dict.fromkeys(collections))
        if not collection_ids:
            return

        enzyme_home = Path(os.environ.get("ENZYME_HOME", Path.home() / ".enzyme"))
        enzyme_home.mkdir(parents=True, exist_ok=True)
        config_path = enzyme_home / "config.toml"
        vault_key = str(vault_path.resolve())
        section = _toml_vault_section(
            vault_key,
            collection_ids,
            self._catalyst_profiles,
        )

        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        body = _replace_toml_vault_section(existing, vault_key, section)
        config_path.write_text(body, encoding="utf-8")

    def disconnect_user(self, user_id: str) -> None:
        self._connected_users.discard(user_id)

    def is_connected(self, user_id: str) -> bool:
        return user_id in self._connected_users

    @property
    def connected_users(self) -> set[str]:
        return set(self._connected_users)

    # -- query --------------------------------------------------------------

    def search(self, user_id: str, query: str, limit: int = 10) -> Any:
        """Search a user's indexed data via enzyme catalyze."""
        if not self.is_connected(user_id):
            raise RuntimeError(f"User {user_id!r} is not connected. Call connect_user() first.")

        ec = self._get_enzyme_client()
        collection_id = self._user_collection_id(user_id)
        store = self._get_store()
        vault_path = str(store.vault_path(collection_id))
        return ec.catalyze(query, vault=vault_path, limit=limit)

    def overview(self, user_id: str, top: int = 10) -> Any:
        """Get the structural overview of a user's data via enzyme petri."""
        if not self.is_connected(user_id):
            raise RuntimeError(f"User {user_id!r} is not connected. Call connect_user() first.")

        ec = self._get_enzyme_client()
        collection_id = self._user_collection_id(user_id)
        store = self._get_store()
        vault_path = str(store.vault_path(collection_id))
        return ec.petri(vault=vault_path, top=top)

    def hosted(
        self,
        user_id: str,
        *,
        base_url: str | None = None,
        timeout: float = 30.0,
        http_client: Any = None,
    ) -> Any:
        """Return a hosted app/user search handle derived from this connector."""
        from enzyme_sdk.hosted import DEFAULT_BASE_URL, HostedScopeClient

        return HostedScopeClient(
            api_key=self.api_key,
            app_id=self.app_id,
            user_id=user_id,
            base_url=base_url or DEFAULT_BASE_URL,
            timeout=timeout,
            http_client=http_client,
        )

    # -- dev mode -----------------------------------------------------------

    def dev(self, entity: str, entries: list[dict[str, Any]]) -> DevSession:
        return DevSession(entity, entries)

    # -- MCP server ---------------------------------------------------------

    def _tool_descriptions(self) -> dict[str, str]:
        """Build tool descriptions. Uses developer overrides if set, otherwise auto-generates."""
        app = self.display_name or "the app"

        descs: dict[str, str] = {}
        for cfg in self._corpora.values():
            descs[cfg.catalyze_tool_name] = cfg.catalyze_description or (
                f"Search the user's {app} {cfg.plural} by concept. The query doesn't "
                "need to match document text — it routes through thematic questions that "
                "characterize this user's patterns. Returns matched documents and the "
                "thematic signals (catalysts) that drove the retrieval."
            )
            descs[cfg.profile_tool_name] = cfg.profile_description or (
                f"Get a structural overview of the user's {app} {cfg.plural} — which "
                "topics are active, what thematic questions characterize each area, and "
                "how their interests have shifted recently."
            )
        return descs


    def as_mcp_app(self, *, whitelist: list[str] | None = None) -> Any:
        """Return a FastAPI app serving JSON-RPC 2.0 MCP tool calls.

        Args:
            whitelist: If set, only these user IDs can be queried (dev mode).
        """
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        allowed = set(whitelist) if whitelist else None
        descs = self._tool_descriptions()

        mcp = FastAPI(
            title=self.display_name or "Enzyme MCP",
            description=self.description,
            version="0.1.0",
        )

        def _build_tools() -> list[dict[str, Any]]:
            tools: list[dict[str, Any]] = []
            for cfg in self._corpora.values():
                tools.append({
                    "name": cfg.catalyze_tool_name,
                    "description": descs.get(cfg.catalyze_tool_name, ""),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "What you're looking for, in natural language. Broad queries work well."},
                            "limit": {"type": "integer", "description": "Max results (1-20).", "default": 10},
                        },
                        "required": ["query"],
                    },
                })
                tools.append({
                    "name": cfg.profile_tool_name,
                    "description": descs.get(cfg.profile_tool_name, ""),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "top": {"type": "integer", "description": "Number of top entities.", "default": 10},
                        },
                    },
                })
            return tools

        def _resolve_user(request) -> str | None:
            """In dev mode, user_id comes from the token or a header."""
            # Dev: X-Enzyme-User header or first whitelisted user
            user = request.headers.get("X-Enzyme-User")
            if user:
                return user
            if allowed:
                return next(iter(allowed))
            # In prod, this would come from the OAuth token
            return None

        def _dispatch(name: str, args: dict[str, Any], user_id: str) -> Any:
            for cfg in self._corpora.values():
                if name == cfg.catalyze_tool_name:
                    result = self.search(user_id, args["query"], args.get("limit", 10))
                    if hasattr(result, "render_to_prompt"):
                        return result.render_to_prompt()
                    return result
                if name == cfg.profile_tool_name:
                    result = self.overview(user_id, args.get("top", 10))
                    if hasattr(result, "render_to_prompt"):
                        return result.render_to_prompt()
                    return result
            raise ValueError(f"Unknown tool: {name!r}")

        @mcp.get("/health")
        async def health():
            return {
                "status": "ok",
                "app": self.display_name,
                "content_label": self.content_label,
                "connected_users": len(self._connected_users),
                "pipeline": "enzyme",
            }

        async def _handler(request) -> JSONResponse:
            import time as _time

            user_id = _resolve_user(request)
            if allowed and user_id not in allowed:
                return JSONResponse(
                    status_code=403,
                    content={"error": f"User {user_id!r} not in whitelist"},
                )

            body = await request.json()
            rpc_id = body.get("id")
            method = body.get("method", "")
            params = body.get("params", {})

            log.info("← %s", method)

            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": self.display_name or "Enzyme",
                        "version": "0.1.0",
                    },
                }
                if self.system_prompt:
                    result["instructions"] = self.system_prompt
                return JSONResponse({"jsonrpc": "2.0", "id": rpc_id, "result": result})

            if method == "tools/list":
                tools = _build_tools()
                log.info("→ tools/list: %s", [t["name"] for t in tools])
                return JSONResponse({
                    "jsonrpc": "2.0", "id": rpc_id,
                    "result": {"tools": tools},
                })

            if method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                if not user_id:
                    return JSONResponse({
                        "jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32000, "message": "Cannot resolve user. Set X-Enzyme-User header."},
                    })
                log.info("→ %s(%s) user=%s", tool_name, arguments, user_id)
                t0 = _time.perf_counter()
                try:
                    result = _dispatch(tool_name, arguments, user_id)
                    elapsed = _time.perf_counter() - t0
                    text = result if isinstance(result, str) else json.dumps(result, default=str, ensure_ascii=False)
                    preview = text[:120] + "…" if len(text) > 120 else text
                    log.info("✓ %s → %d chars in %.2fs: %s", tool_name, len(text), elapsed, preview)
                    return JSONResponse({
                        "jsonrpc": "2.0", "id": rpc_id,
                        "result": {"content": [{"type": "text", "text": text}]},
                    })
                except (RuntimeError, ValueError) as exc:
                    elapsed = _time.perf_counter() - t0
                    log.warning("✗ %s failed in %.2fs: %s", tool_name, elapsed, exc)
                    code = -32000 if isinstance(exc, RuntimeError) else -32601
                    return JSONResponse({
                        "jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": code, "message": str(exc)},
                    })

            return JSONResponse({
                "jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"Unknown method: {method!r}"},
            })

        # PEP 563 workaround: patch annotation so FastAPI sees the real Request class
        from fastapi import Request as _Req
        _handler.__annotations__["request"] = _Req
        mcp.post("/mcp")(_handler)

        return mcp

    # -- serve (dev mode with optional ngrok) -------------------------------

    def serve(
        self,
        port: int = 9460,
        *,
        init_users: list[str] | None = None,
        ngrok: bool = False,
        ngrok_domain: str | None = None,
    ) -> None:
        """Start the MCP server for development / testing.

        Args:
            port: Local port.
            init_users: User IDs to hydrate + index on startup. Whitelisted for queries.
            ngrok: If True, expose via ngrok tunnel for testing with Claude.
            ngrok_domain: Custom ngrok domain (requires paid plan).
        """
        # Configure logging so tool call logs are visible
        logging.basicConfig(
            level=logging.INFO,
            format="  %(name)s  %(message)s",
        )
        log.setLevel(logging.INFO)

        if init_users:
            for uid in init_users:
                self.connect_user(uid)

        mcp_app = self.as_mcp_app(whitelist=init_users)
        url = f"http://localhost:{port}"

        print()
        print("=" * 64)
        print(f"  {self.display_name or 'Enzyme'} MCP Server")
        print("=" * 64)
        print(f"  Local:    {url}")

        tunnel_url = None
        if ngrok:
            tunnel_url = self._start_ngrok(port, ngrok_domain)
            if tunnel_url:
                print(f"  Public:   {tunnel_url}")
                print()
                print("  Add to Claude → Settings → Connectors:")
                print(f"    URL: {tunnel_url}/mcp")

        print()
        if init_users:
            for uid in init_users:
                count = len(self._user_stores.get(uid, []))
                print(f"  {uid}: {count} entries")
        print()

        pipeline = "enzyme catalyze"
        print(f"  Search:   {pipeline}")
        print(f"  Content:  {self.content_label}")

        if self.system_prompt:
            print(f"  Prompt:   {self.system_prompt[:60]}...")
        print()
        print("-" * 64)
        print("  curl examples")
        print("-" * 64)
        print()

        base = tunnel_url or url
        user_header = ""
        if init_users:
            user_header = f'\n    -H "X-Enzyme-User: {init_users[0]}" \\'

        print(f"  # Health")
        print(f"  curl {base}/health")
        print()
        print(f"  # List tools")
        print(f"""  curl -X POST {base}/mcp \\
    -H "Authorization: Bearer dev-token" \\{user_header}
    -H "Content-Type: application/json" \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"tools/list"}}'""")
        print()

        for cfg in self._corpora.values():
            print(f"  # Catalyze {cfg.plural}")
            print(f"""  curl -X POST {base}/mcp \\
    -H "Authorization: Bearer dev-token" \\{user_header}
    -H "Content-Type: application/json" \\
    -d '{{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{{"name":"{cfg.catalyze_tool_name}","arguments":{{"query":"your query here"}}}}}}'""")
            print()

        print("=" * 64)
        print()

        import uvicorn
        uvicorn.run(mcp_app, host="0.0.0.0", port=port)

    @staticmethod
    def _start_ngrok(port: int, domain: str | None = None) -> str | None:
        """Start an ngrok tunnel. Returns the public URL or None."""
        try:
            from pyngrok import ngrok as _ngrok
            kwargs: dict[str, Any] = {"addr": str(port), "proto": "http"}
            if domain:
                kwargs["hostname"] = domain
            tunnel = _ngrok.connect(**kwargs)
            return str(tunnel.public_url)
        except ImportError:
            print("  (ngrok requested but pyngrok not installed: pip install pyngrok)")
            return None
        except Exception as exc:
            print(f"  (ngrok failed: {exc})")
            return None

    def __repr__(self) -> str:
        return (
            f"<EnzymeConnector {self.display_name!r} "
            f"content_label={self.content_label!r} connected={len(self._connected_users)}>"
        )


# ---------------------------------------------------------------------------
# Decorator namespace
# ---------------------------------------------------------------------------

class _Enzyme:
    """Decorator namespace — ``enzyme.hydrate`` and ``enzyme.on_save``."""

    def hydrate(
        self,
        client: EnzymeConnector,
    ) -> Callable:
        """Register a function that loads a user's data for indexing.

        Called when a user connects. Must accept ``user_id`` as first arg
        and return a list of dicts, each with at least ``title`` and
        ``content`` keys.

        Tool names and descriptions are configured on ``EnzymeConnector``.
        """
        def decorator(fn: Callable) -> Callable:
            client._register_hydrate(fn)
            return fn
        return decorator

    def on_save(
        self,
        client: EnzymeConnector,
        *,
        title: str | Callable = "title",
        content: str | Callable = "content",
        tags: str | Callable | None = "tags",
        created_at: str | Callable | None = None,
        primitive: str | Callable | None = None,
        source_id: str | Callable | None = None,
        metadata: str | Callable | None = None,
        map: Callable | None = None,
    ) -> Callable:
        """Decorate an existing save function — Enzyme indexes the return value.

        The decorated function's signature and return value are **unchanged**.
        The decorator extracts enzyme fields from the return value using either
        the field-name shortcuts or a custom ``map`` callable.

        Field shortcuts (default):
            ``title="title"`` means ``entry["title"]`` on the return dict.
            Pass a callable for complex extraction: ``title=lambda r: r.name``.

        Custom map (overrides field shortcuts):
            ``map=lambda r: {"title": r.name, "content": r.body, "tags": r.tags}``

        Example::

            @enzyme.on_save(client, title="title", content="instructions", tags="tags")
            def create_recipe(user_id, data):
                recipe = db.insert(data)
                return recipe   # unchanged — enzyme extracts what it needs
        """
        if map is not None:
            field_map: dict[str, str | Callable] = {"_map": map}
        else:
            field_map = {"title": title, "content": content}
            if tags is not None:
                field_map["tags"] = tags
            if created_at is not None:
                field_map["created_at"] = created_at
            if primitive is not None:
                field_map["primitive"] = primitive
            if source_id is not None:
                field_map["source_id"] = source_id
            if metadata is not None:
                field_map["metadata"] = metadata

        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(user_id: str, *args: Any, **kwargs: Any) -> Any:
                result = fn(user_id, *args, **kwargs)
                if client.is_connected(user_id) and result is not None:
                    entry = client._entry_from_item(result)
                    client._queue_ingest(user_id, entry)
                return result
            client._register_save(wrapper, field_map)
            return wrapper
        return decorator

    def transform(
        self,
        client: EnzymeConnector,
    ) -> Callable:
        """Register a typed source-item transform for hydrate and save ingest.

        The transform receives the app-native item returned by ``@hydrate`` or
        ``@on_save`` and returns an ``Activity`` or an equivalent entry dict.
        """
        def decorator(fn: Callable) -> Callable:
            client._register_transform(fn)
            return fn
        return decorator

    def collection(
        self,
        client: EnzymeConnector,
    ) -> Callable:
        """Register how a typed source item maps to a connector collection."""
        def decorator(fn: Callable) -> Callable:
            client._register_collection(fn)
            return fn
        return decorator

    # Keep backwards compat alias
    fetch = hydrate


enzyme = _Enzyme()
