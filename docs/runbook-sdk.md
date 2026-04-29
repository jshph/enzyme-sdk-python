# Runbook: enzyme-sdk (Python SDK)

Python SDK providing hosted Enzyme clients for published vault search and
app/user scoped product integrations.

## What It Does

Instead of running the enzyme CLI binary locally, these clients call the hosted
search API at `app.enzyme.garden`.

`HostedScopeClient` is the product integration surface. It searches across one
application user's scope, where the service can combine multiple internal ingest
collections such as conversations, interactions, artifacts, folders, or emails.
Normal `catalyze()` results hide those storage partitions and return
app-hydratable primitives through `primitive` and `source_id`.

`HostedEnzymeClient` remains the legacy published-vault surface for
`/v1/vaults/{slug}`.

## Key Files

| File | Purpose |
|---|---|
| `enzyme_sdk/hosted.py` | `HostedScopeClient` and legacy `HostedEnzymeClient` |
| `tests/test_hosted_scope.py` | Unit tests for app/user scoped hosted API semantics |
| `tests/test_hosted.py` | Legacy published-vault integration tests (require local search service running) |
| `pyproject.toml` | Package config — `enzyme-sdk` on PyPI |

## API

### Product integrations

```python
from enzyme_sdk.hosted import HostedScopeClient

client = HostedScopeClient(
    api_key="enz_...",
    app_id="chat-app",
    user_id="user_123",
    base_url="https://app.enzyme.garden",  # default
)

# Global catalyst-mediated search across the user's app scope.
response = client.catalyze("customer handoff", limit=10, register="explore")

for result in response.results:
    # Hydrate in your app by primitive + source_id.
    print(result.primitive, result.source_id, result.title)

# Entity overview for the full scope.
entities = client.petri(top=10, query="launch")

# Scope health includes internal collection generation/count metadata.
status = client.status()

# Refreshes the whole scope. Collection partitioning is service/internal.
client.refresh()

client.close()  # or use as context manager
```

Use `HostedScopeClient` when building a chat app, email client, CRM, research
tool, or other application where Enzyme should connect cross-cutting primitives
across a user's data. Do not expose collection ids in normal user-facing search
UI; they are ingest/cache partitions, not relevance semantics.

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

Scoped SDK unit tests do not require a running service:

```bash
pytest tests/test_hosted_scope.py -q
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
