# Runbook: enzyme-sdk (Python SDK)

Python SDK providing the connector integration surface plus hosted Enzyme
clients for published vault search and app/user scoped product integrations.

## What It Does

Instead of running the enzyme CLI binary locally, these clients call the hosted
search API at `app.enzyme.garden`.

`EnzymeConnector` is the product integration surface. Its decorators hydrate
app-native items and transform them into Enzyme `Activity` payloads, including
`collection` or `collections` for the per-user ingest/cache partition. Hosted
app/user search is reached through
`connector.hosted(user_id)`, where the service can combine multiple internal
collections such as recipe comments, saved recipes, conversations, artifacts,
folders, or emails. Normal `catalyze()` results hide those storage partitions
and return app-hydratable activities with `source_id`.

`HostedScopeClient` still exists in `enzyme_sdk.hosted` as the low-level HTTP
transport used by `EnzymeConnector.hosted(...)`; it is not the main integration
surface.

`HostedEnzymeClient` remains the legacy published-vault surface for
`/v1/vaults/{slug}`.

## Key Files

| File | Purpose |
|---|---|
| `enzyme_sdk/enzyme.py` | `EnzymeConnector`, decorators, transform mapping, and hosted scope entry |
| `enzyme_sdk/activity.py` | `Activity` payload emitted by connector transforms |
| `enzyme_sdk/hosted.py` | Low-level hosted scope transport and legacy `HostedEnzymeClient` |
| `tests/test_connector_collections.py` | Unit tests for declarative collection mapping and connector-hosted entry |
| `tests/test_hosted_scope.py` | Unit tests for app/user scoped hosted API semantics |
| `tests/test_hosted.py` | Legacy published-vault integration tests (require local search service running) |
| `pyproject.toml` | Package config — `enzyme-sdk` on PyPI |

## API

### Product integrations

```python
from dataclasses import dataclass

from enzyme_sdk import Activity, EnzymeConnector, enzyme

@dataclass
class CookingEvent:
    id: str
    user_id: str
    kind: str
    recipe_name: str
    comment: str
    date: str
    source_tags: list[str]
    auto_tags: list[str]

connector = EnzymeConnector(
    api_key="enz_...",
    app_id="nyt-cooking",
    display_name="NYT Cooking Notes",
)

@enzyme.transform(connector)
def cooking_activity(event: CookingEvent) -> Activity:
    return Activity(
        title=event.recipe_name,
        content=event.comment,
        created_at=event.date,
        source_id=event.id,
        collections=[f"recipe/{tag}" for tag in event.source_tags] or [event.kind],
        metadata={
            "activity_type": event.kind,
            "labels": [*event.source_tags, *event.auto_tags],
        },
    )

@enzyme.on_save(connector)
def save_activity(user_id: str, event: CookingEvent) -> CookingEvent:
    return db.save(event)

client = connector.hosted("user_123")

# Global catalyst-mediated search across the user's app scope.
response = client.catalyze("quick weeknight dinners with ginger", limit=10, register="explore")

for result in response.results:
    # Hydrate in your app by source_id.
    print(result.source_id, result.title)

# Entity overview for the full scope.
entities = client.petri(top=10, query="launch")

# Scope health includes internal collection generation/count metadata.
status = client.status()

# Refreshes the whole scope. Collection partitioning is service/internal.
client.refresh()

client.close()  # or use as context manager
```

Use `EnzymeConnector` when building a cooking app, chat app, email client, CRM,
research tool, or other application where Enzyme should connect cross-cutting
activities across a user's data. Do not expose collection ids in normal
user-facing search UI; they are ingest/cache partitions, not relevance
semantics.

### Published vaults

```python
from enzyme_sdk.hosted import HostedEnzymeClient

client = HostedEnzymeClient(
    api_key="enz_...",          # Bearer token (currently unused by search service)
    vault_slug="abc123-vault",  # Slug from enzyme publish
    base_url="https://app.enzyme.garden",  # default
)

# Semantic search
results = client.catalyze("design patterns", limit=10, register="explore")
# -> list[HostedCatalyzeResult] with .catalyst, .entity, .documents

# Entity overview
entities = client.petri(top=10, query="architecture")
# -> list[HostedPetriEntity] with .name, .type, .frequency, .catalysts

# Vault stats
status = client.status()
# -> HostedVaultStatus with .docs, .entities, .catalysts, .embeddings

client.close()  # or use as context manager
```

## Install

```bash
# Development
pip install -e ".[dev]"

# Production
pip install enzyme-sdk
```

Dependencies: `httpx`, `fastapi`, `uvicorn`, `pydantic`.

## Testing

Scoped SDK and connector mapping unit tests do not require a running service:

```bash
pytest tests/test_connector_collections.py tests/test_hosted_scope.py -q
```

Tests require a local search service with a published vault:

```bash
# 1. Start the stack (see infra/e2e-test.sh in enzyme-rust)
# 2. Publish a test vault to get a slug
# 3. Run tests
ENZYME_SEARCH_URL=http://localhost:8766 \
ENZYME_TEST_SLUG=<your-slug> \
pytest tests/test_hosted.py -v
```

Or run the full e2e test from enzyme-rust which includes SDK tests:
```bash
cd ../enzyme-rust && ./infra/e2e-test.sh
```

## Quick Debug

1. **`httpx.ConnectError`** — Search service is not running or `base_url` is wrong. Default is `https://app.enzyme.garden`. For local dev, pass `base_url="http://localhost:8766"`.

2. **404 on scope endpoints** — Check `app_id` and `user_id`. Scoped integrations use `/v1/scopes/{app_id}/{user_id}`.

3. **404 on legacy vault endpoints** — Wrong `vault_slug`. The slug is `{user_id_prefix}-{vault_name}` and is shown in the `enzyme publish` output. Check with `GET /v1/vaults/{slug}/status`.

4. **Legacy tests skipped** — Tests auto-skip if the search service isn't reachable at `ENZYME_SEARCH_URL`. Start the local stack first.
