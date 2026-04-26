"""HostedEnzymeClient — calls the enzyme hosted REST API."""

from __future__ import annotations

import httpx
from dataclasses import dataclass, field
from typing import Any


DEFAULT_BASE_URL = "https://search.enzyme.garden"


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
