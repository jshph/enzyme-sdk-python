"""HostedEnzymeClient — calls the enzyme hosted REST API."""

from __future__ import annotations

import httpx
from dataclasses import dataclass, field
from typing import Any


DEFAULT_BASE_URL = "https://app.enzyme.garden"


@dataclass
class HostedCatalyzeResult:
    """A search result from the hosted API."""
    catalyst: str
    entity: str
    documents: list[dict[str, Any]]


@dataclass
class HostedPetriEntity:
    """An entity from the hosted petri endpoint."""
    name: str
    type: str
    frequency: int
    catalysts: list[str]


@dataclass
class HostedVaultStatus:
    """Vault statistics from the hosted API."""
    docs: int
    entities: int
    catalysts: int
    embeddings: int


@dataclass
class HostedScopeCatalyst:
    """A catalyst returned from app/user scoped hosted search."""

    entity: str
    text: str
    relevance: float
    contribution_count: int = 0


@dataclass
class HostedScopeResult:
    """An app-hydratable source result from hosted scope search."""

    primitive: str
    source_id: str
    title: str
    snippet: str
    app_url: str | None = None
    created_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    similarity: float = 0.0


@dataclass
class HostedScopeSearchResponse:
    """Full response from app/user scoped hosted catalyze."""

    scope: str
    query: str
    catalysts: list[HostedScopeCatalyst]
    results: list[HostedScopeResult]
    total: int
    register: str = "explore"
    scope_fingerprint: str = ""
    debug: dict[str, Any] | None = None


@dataclass
class HostedScopeEntity:
    """An entity from app/user scoped hosted petri."""

    name: str
    type: str
    frequency: int
    catalysts: list[dict[str, Any]]
    frequency_12m: int = 0
    recency_score: float = 0.0
    activity_trend: str = ""
    days_since_last_seen: int | None = None
    last_seen: int | None = None


@dataclass
class HostedScopeCollectionStatus:
    """Internal hosted collection status used for scope health/debug."""

    name: str
    index_generation: int
    counts: dict[str, int]


@dataclass
class HostedScopeStatus:
    """Status for an app/user hosted search scope."""

    scope: str
    scope_fingerprint: str
    totals: dict[str, int]
    collections: list[HostedScopeCollectionStatus] = field(default_factory=list)


class HostedEnzymeClient:
    """Client for the enzyme hosted search API.

    Instead of running the enzyme CLI binary locally, this client calls
    the hosted REST API to search and browse published vaults.

    Usage::

        client = HostedEnzymeClient(api_key="enz_...", vault_slug="abc123-my-vault")
        results = client.catalyze("design patterns")
        overview = client.petri(top=10)
        stats = client.status()
    """

    def __init__(
        self,
        api_key: str,
        vault_slug: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
    ):
        self.api_key = api_key
        self.vault_slug = vault_slug
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self.base_url}/v1/vaults/{vault_slug}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def catalyze(
        self,
        query: str,
        limit: int = 10,
        register: str = "explore",
    ) -> list[HostedCatalyzeResult]:
        """Search the vault by concept/theme.

        Args:
            query: Natural language search query.
            limit: Maximum number of results.
            register: Presentation register (explore, continuity, reference).

        Returns:
            List of search results with catalysts and matching documents.
        """
        resp = self._client.post(
            "/search",
            json={"query": query, "limit": limit, "register": register},
        )
        resp.raise_for_status()
        data = resp.json()
        return [
            HostedCatalyzeResult(
                catalyst=r.get("catalyst", ""),
                entity=r.get("entity", ""),
                documents=r.get("documents", []),
            )
            for r in data.get("results", [])
        ]

    def petri(
        self,
        top: int = 10,
        query: str | None = None,
    ) -> list[HostedPetriEntity]:
        """Browse vault entities and their catalysts.

        Args:
            top: Number of top entities to return.
            query: Optional query to rank entities by relevance.

        Returns:
            List of entities with their catalysts.
        """
        params: dict[str, Any] = {"top": top}
        if query:
            params["query"] = query
        resp = self._client.get("/petri", params=params)
        resp.raise_for_status()
        data = resp.json()
        return [
            HostedPetriEntity(
                name=e.get("name", ""),
                type=e.get("type", ""),
                frequency=e.get("frequency", 0),
                catalysts=e.get("catalysts", []),
            )
            for e in data.get("entities", [])
        ]

    def status(self) -> HostedVaultStatus:
        """Get vault statistics.

        Returns:
            Vault statistics (doc count, entity count, etc).
        """
        resp = self._client.get("/status")
        resp.raise_for_status()
        data = resp.json()
        return HostedVaultStatus(
            docs=data.get("docs", 0),
            entities=data.get("entities", 0),
            catalysts=data.get("catalysts", 0),
            embeddings=data.get("embeddings", 0),
        )

    def close(self):
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class HostedScopeClient:
    """Client for app/user scoped hosted search.

    This is the product-integration hosted API. It targets
    ``/v1/scopes/{app_id}/{user_id}``, not the legacy published-vault routes.

    Normal search results intentionally omit storage collection ids. The app
    should hydrate by ``primitive`` and ``source_id``; collection/generation
    details are available through ``status()`` and optional debug responses.
    """

    def __init__(
        self,
        api_key: str,
        app_id: str,
        user_id: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        *,
        http_client: httpx.Client | None = None,
    ):
        self.api_key = api_key
        self.app_id = app_id
        self.user_id = user_id
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(
            base_url=f"{self.base_url}/v1/scopes/{app_id}/{user_id}",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def catalyze(
        self,
        query: str,
        limit: int = 10,
        register: str = "explore",
        *,
        debug: bool = False,
    ) -> HostedScopeSearchResponse:
        """Search across this app user's hosted scope.

        The hosted service resolves the user's participating ingest partitions
        internally and runs one global catalyst-mediated search across the scope.
        """
        resp = self._client.post(
            "/catalyze",
            json={
                "query": query,
                "limit": limit,
                "register": register,
                "debug": debug,
            },
        )
        resp.raise_for_status()
        return _parse_scope_search_response(resp.json())

    def petri(self, top: int = 10, query: str | None = None) -> list[HostedScopeEntity]:
        """Browse the scope's current entities and catalysts."""
        params: dict[str, Any] = {"top": top}
        if query:
            params["query"] = query
        resp = self._client.get("/petri", params=params)
        resp.raise_for_status()
        data = resp.json()
        return [_parse_scope_entity(e) for e in data.get("entities", [])]

    def status(self) -> HostedScopeStatus:
        """Get scope health and internal collection generation status."""
        resp = self._client.get("/status")
        resp.raise_for_status()
        data = resp.json()
        return HostedScopeStatus(
            scope=data.get("scope", ""),
            scope_fingerprint=data.get("scope_fingerprint", ""),
            totals={
                "docs": int(data.get("totals", {}).get("docs", 0)),
                "entities": int(data.get("totals", {}).get("entities", 0)),
                "catalysts": int(data.get("totals", {}).get("catalysts", 0)),
                "embeddings": int(data.get("totals", {}).get("embeddings", 0)),
            },
            collections=[
                HostedScopeCollectionStatus(
                    name=c.get("name", ""),
                    index_generation=int(c.get("index_generation", 0)),
                    counts={
                        "docs": int(c.get("counts", {}).get("docs", 0)),
                        "entities": int(c.get("counts", {}).get("entities", 0)),
                        "catalysts": int(c.get("counts", {}).get("catalysts", 0)),
                        "embeddings": int(c.get("counts", {}).get("embeddings", 0)),
                    },
                )
                for c in data.get("collections", [])
            ],
        )

    def refresh(self) -> dict[str, Any]:
        """Refresh the full hosted scope.

        Collection names are intentionally not part of this public SDK method;
        storage partition refresh remains a service/internal concern.
        """
        resp = self._client.post("/refresh", json={})
        resp.raise_for_status()
        return resp.json()

    def close(self):
        """Close the underlying HTTP client if this instance owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _parse_scope_search_response(data: dict[str, Any]) -> HostedScopeSearchResponse:
    return HostedScopeSearchResponse(
        scope=data.get("scope", ""),
        scope_fingerprint=data.get("scope_fingerprint", ""),
        query=data.get("query", ""),
        register=data.get("register", "explore"),
        catalysts=[
            HostedScopeCatalyst(
                entity=c.get("entity", ""),
                text=c.get("text", ""),
                relevance=float(c.get("relevance", 0.0)),
                contribution_count=int(c.get("contribution_count", 0)),
            )
            for c in data.get("catalysts", [])
        ],
        results=[
            HostedScopeResult(
                primitive=r.get("primitive", ""),
                source_id=r.get("source_id", ""),
                title=r.get("title", ""),
                snippet=r.get("snippet", ""),
                app_url=r.get("app_url"),
                created_at=r.get("created_at"),
                metadata=r.get("metadata", {}) or {},
                similarity=float(r.get("similarity", 0.0)),
            )
            for r in data.get("results", [])
        ],
        total=int(data.get("total", 0)),
        debug=data.get("cache"),
    )


def _parse_scope_entity(data: dict[str, Any]) -> HostedScopeEntity:
    catalysts = data.get("catalysts", [])
    normalized = [{"text": c} if isinstance(c, str) else c for c in catalysts]
    return HostedScopeEntity(
        name=data.get("name", ""),
        type=data.get("type", ""),
        frequency=int(data.get("frequency", 0)),
        frequency_12m=int(data.get("frequency_12m", 0)),
        recency_score=float(data.get("recency_score", 0.0)),
        activity_trend=data.get("activity_trend", ""),
        days_since_last_seen=data.get("days_since_last_seen"),
        last_seen=data.get("last_seen"),
        catalysts=normalized,
    )
