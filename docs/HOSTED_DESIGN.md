# Enzyme Hosted: Multi-Tenant Connector-as-a-Service Design

## One-Liner

Decorate your save functions. Enzyme turns them into a Claude connector — with OAuth, user isolation, and catalyst-based personalization. No data moves until the user opts in.

---

## Developer Experience: Try It In 60 Seconds

### Dev mode: inline data → working MCP URL

Before integrating with your app, test with sample data:

```python
from enzyme_sdk import EnzymeHosted

client = EnzymeHosted(api_key="enz_live_...")

# Provide sample data inline — no app integration needed
dev = client.dev(
    entity="recipe",
    entries=[
        {"title": "Mushroom Risotto", "content": "Arborio rice, mushrooms...", "tags": ["vegetarian"]},
        {"title": "Thai Green Curry", "content": "Coconut milk, green paste...", "tags": ["thai", "spicy"]},
        {"title": "Sourdough Bread", "content": "Flour, water, starter...", "tags": ["baking"]},
    ],
)

print(dev.mcp_url)
# → https://app.enzyme.garden/dev-abc123/mcp
# Paste this into Claude Settings → Connectors → Add custom connector
# No OAuth — dev URLs are open. Claude can immediately query the data.

# Preview what Claude will see
print(dev.status())    # { docs: 3, entities: 4, catalysts: 12 }
print(dev.search("comfort food"))  # test a query locally
```

The `dev()` method:
1. Creates an ephemeral Turso DB with your sample data
2. Indexes it immediately (embed, cluster, catalyze)
3. Returns a working MCP URL — paste into Claude and test
4. No OAuth required — dev URLs skip auth
5. Auto-expires after 24 hours (or `dev(ttl_hours=72)`)

**Scale up incrementally:**

```python
# Test with one real user's data (from your DB)
dev = client.dev(entity="recipe", fetch=lambda: get_user_recipes("user-123"))

# Test with multiple users
dev = client.dev(entity="recipe", users={
    "alice": get_user_recipes("user-123"),
    "bob": get_user_recipes("user-456"),
})
# dev.mcp_url works for all test users (select user in Claude via a dev tool)
```

---

### Production integration (3 decorators)

```python
from enzyme_sdk import enzyme, EnzymeHosted

client = EnzymeHosted(
    api_key="enz_live_...",
    display_name="DishGen",
    description="Your saved recipes and cooking history",
    logo_url="https://dishgen.com/logo.png",
)

# 1. Declare what a "recipe" looks like when saved
@enzyme.on_save(client, entity="recipe")
def save_recipe(user_id: str, recipe: dict) -> dict:
    return {
        "title": recipe["title"],
        "content": recipe["instructions"],
        "tags": recipe.get("dietary_labels", []),
        "metadata": {"rating": recipe.get("rating")},
    }

# 2. Declare how to fetch all recipes for a user (bulk pull on first connection)
@enzyme.fetch(client, entity="recipe")
def get_recipes(user_id: str) -> list[dict]:
    return [
        {"title": r.title, "content": r.instructions, "tags": r.labels}
        for r in db.query(Recipe).filter_by(user_id=user_id)
    ]

# 3. Your existing app code — unchanged
@app.post("/api/recipes")
def create_recipe(request):
    recipe = Recipe.create(**request.json)
    save_recipe(request.user_id, recipe.to_dict())  # Enzyme intercepts this
    return recipe
```

That's the entire integration. The decorators ARE the data contract.

### What the decorators do

**`@enzyme.on_save`**: Intercepts the function call. If the user is connected to Claude, queues the returned dict for incremental ingest. If not connected, does nothing (no-op passthrough).

**`@enzyme.fetch`**: Registers a bulk data source. Enzyme calls this when a user first connects via Claude — pulls all their data, indexes it. Also called on periodic refresh.

**No `register_user`, no `configure_tenant`, no `data_endpoint` URL.** The decorators declare everything Enzyme needs. The SDK auto-registers the app as a tenant on first call, discovers users from the decorated function signatures, and serves as both the data contract and the fetch mechanism.

---

## Testing & Sampling

### Preview: what would Enzyme see for a user?

```python
# Preview the index for a specific user (dry run, no connection needed)
preview = client.preview("user-123")

preview.entries      # 47 entries that @enzyme.fetch would return
preview.entities     # ['vegetarian', 'italian', 'thai', 'baking', ...]
preview.catalysts    # ['How does this user balance health goals with comfort food?', ...]
preview.doc_count    # 47
preview.top_tags     # [('vegetarian', 23), ('italian', 15), ('thai', 8)]
```

This runs the fetch function locally, indexes in-memory, and returns what Claude would see — without creating any cloud resources.

### Sample: map-reduce across users

```python
# Sample a cohort to understand what Enzyme would surface
sample = client.sample(
    user_ids=["user-123", "user-456", "user-789"],
    # or: sample_size=100 (random sample from registered users)
)

sample.per_user          # [{ user_id, doc_count, entity_count, top_catalysts }, ...]
sample.common_entities   # entities appearing across 50%+ of users
sample.unique_entities   # entities specific to individual users
sample.catalyst_themes   # top catalyst themes across the cohort
sample.avg_doc_count     # 52.3
sample.median_doc_count  # 41
sample.schema_coverage   # {'title': 1.0, 'content': 1.0, 'tags': 0.87, 'metadata': 0.65}
```

This helps the app company understand:
- Is the data rich enough for catalyst generation?
- What patterns will Enzyme discover?
- Are there schema gaps (missing fields)?

### Validate: check the data contract

```python
# Validate that the decorated functions return the expected shape
report = client.validate()

report.entities         # ['recipe'] — from @enzyme.on_save declarations
report.fetch_functions  # ['get_recipes'] — from @enzyme.fetch declarations
report.schema           # inferred from return types
report.issues           # ['get_recipes: user-999 returned 0 entries', ...]
```

---

## How It Works (End-to-End)

```
App Company (DishGen)              Enzyme Hosted                    Claude
┌─────────────────┐    ┌─────────────────────────────┐    ┌──────────────┐
│                  │    │                             │    │              │
│  @enzyme.on_save │    │  Tenant auto-registered     │    │              │
│  @enzyme.fetch   │    │  from decorator metadata    │    │              │
│                  │    │                             │    │              │
│  App runs        │    │  Users discovered from      │    │              │
│  normally        │    │  function calls (lazy)      │    │              │
│                  │    │                             │    │              │
│                  │    │         ┌───────────────┐   │    │              │
│                  │    │         │ User connects │◀──┼────│  OAuth flow  │
│                  │    │         │ in Claude     │   │    │              │
│                  │    │         └───────┬───────┘   │    │              │
│                  │    │                 │           │    │              │
│  @enzyme.fetch   │◀──┼── Enzyme calls  │           │    │              │
│  returns entries │──▶│  fetch function │           │    │              │
│                  │    │                 ▼           │    │              │
│                  │    │  Index pipeline             │    │              │
│                  │    │  → embed, cluster, catalyze │    │              │
│                  │    │  → Turso DB-per-user        │    │              │
│                  │    │                             │    │              │
│  @enzyme.on_save │──▶│  Incremental updates        │◀──▶│  Tool calls  │
│  (after connect) │    │  (queued, async)            │    │  (search,    │
│                  │    │                             │    │   overview)  │
└─────────────────┘    └─────────────────────────────┘    └──────────────┘
```

### Key Principle: No Data Until Consent

1. **App decorates functions** — declares data shape and fetch logic. Zero data moves.
2. **App runs normally** — `on_save` is a no-op until user connects.
3. **User connects in Claude** — OAuth flow, explicit consent.
4. **Enzyme calls `@fetch`** — pulls user's data, indexes it.
5. **`@on_save` activates** — future saves are incrementally ingested.
6. **Claude queries** — tool calls hit the indexed data.

No Turso DB, no embeddings, no LLM spend until the user opts in.

---

## OAuth: Two Models

### Enzyme-Managed (default)

Enzyme hosts consent. User signs in with Google/GitHub. Enzyme matches by email to the app's users.

```python
client = EnzymeHosted(
    api_key="enz_live_...",
    oauth_mode="enzyme_managed",  # default
)
```

User mapping: when the `@enzyme.fetch` function is called with a `user_id`, Enzyme matches that user_id to the Supabase-authenticated email. The app should use consistent email addresses.

### Delegated (app controls auth)

App runs their own OAuth. Enzyme validates tokens via JWKS.

```python
client = EnzymeHosted(
    api_key="enz_live_...",
    oauth_mode="delegated",
    oauth_config={
        "authorization_endpoint": "https://auth.dishgen.com/authorize",
        "token_endpoint": "https://auth.dishgen.com/token",
        "jwks_uri": "https://auth.dishgen.com/.well-known/jwks.json",
        "user_id_claim": "dishgen_user_id",
    },
)
```

---

## MCP Server: Per-Tenant

```
https://app.enzyme.garden/{tenant_slug}/mcp
```

### Tool Definitions

Auto-generated from `@enzyme.on_save` entity names and the data schema:

| Entity | Generated Tools |
|--------|----------------|
| `recipe` | `search_recipes`, `get_recipe_profile` |
| `journal_entry` | `search_journal`, `get_journal_profile` |
| `workout` | `search_workouts`, `get_workout_profile` |

Customizable:
```python
client = EnzymeHosted(
    tool_config={
        "search": {"name": "find_recipes", "description": "Search saved recipes by concept."},
        "overview": {"name": "cooking_profile", "description": "Overview of cooking patterns."},
    },
)
```

### Discovery (RFC 9728)

`GET /.well-known/protected-resource/{tenant}` → OAuth metadata → Claude triggers auth flow.

Both `claude.ai` and `claude.com` callback URLs pre-configured.

---

## Data Isolation: DB-per-User on Turso

One Turso database per connected user. Created lazily on first connection.

```
Turso Group: enzyme-vaults
├── enzyme-meta (tenants, users, oauth, api_keys)
├── dishgen--schema (parent template, no data)
│   ├── dishgen--user-123 (created on connection)
│   ├── dishgen--user-456 (created on connection)
│   └── ... (only connected users)
└── ...
```

### User Lifecycle

| Event | State | Turso DB | Data |
|-------|-------|----------|------|
| `@on_save` called for user | `discovered` | None | None |
| User connects in Claude | `connecting` | Created (child of schema parent) | `@fetch` called, indexing |
| Index complete | `ready` | Populated | Searchable |
| `@on_save` called (post-connect) | `ready` | Updated incrementally | Latest |
| User disconnects | `disconnected` | Retained | Not served |
| User requests deletion | `deleted` | Dropped | Gone |

---

## Connector Directory Submission

### What Enzyme pre-fills

| Field | Source |
|-------|--------|
| Server URL | `https://app.enzyme.garden/{tenant}/mcp` |
| Server name | `client.display_name` |
| Auth type | OAuth 2.0 |
| Logo | `client.logo_url` |
| Tool inventory | Auto-generated from `@enzyme.on_save` entities |
| Callback URLs | Both `claude.ai` and `claude.com` |
| Read-only hints | All tools are `readOnlyHint: true` |

### What the app provides

```python
submission = client.prepare_submission(
    privacy_policy_url="https://dishgen.com/privacy",
    documentation_url="https://dishgen.com/docs/claude",
    test_account={"email": "reviewer@dishgen.com", "password": "..."},
    category="Food & Recipes",
)
# submission.summary  → review before submitting at claude.com/connectors/submit
```

---

## SDK Internals

### How the decorator works

```python
@enzyme.on_save(client, entity="recipe")
def save_recipe(user_id: str, recipe: dict) -> dict:
    ...
```

This wraps `save_recipe` to:

1. **On first call**: Auto-register the tenant with Enzyme Hosted (if not already). Send entity name + inferred schema.
2. **On every call**: Check if `user_id` has a connected Claude session (local cache, refreshed periodically). If yes → queue the returned dict for async ingest. If no → passthrough (no-op).
3. **Register the function** as a fetch source. The SDK exposes the wrapped function via a lightweight HTTP server (or the app's existing server) so Enzyme can call it remotely for bulk pulls.

### How fetch works

```python
@enzyme.fetch(client, entity="recipe")
def get_recipes(user_id: str) -> list[dict]:
    ...
```

This registers `get_recipes` as the bulk data source for the `recipe` entity. When Enzyme needs to pull a user's data (on connection or refresh), it calls this function.

**Two modes for how Enzyme calls it:**

1. **Embedded mode** (default): The SDK runs a lightweight callback server within the app process. Enzyme calls `POST {app_callback_url}/enzyme/fetch` with `{user_id, entity}`. The SDK dispatches to the registered `@fetch` function and returns entries.

2. **Webhook mode**: The app exposes the endpoint themselves. SDK provides the handler: `enzyme.webhook_handler(app)` adds the route to Flask/FastAPI/Express.

### Connection state cache

The SDK maintains a local cache of connected user IDs (refreshed every 30 seconds from Enzyme's API). This makes `@on_save` checks fast — no network call per save.

```python
# Internal: SDK periodically syncs
connected_users = client._sync_connected_users()
# Returns: {'user-123', 'user-456'}

# On save_recipe("user-123", ...):
# Check: "user-123" in connected_users → yes → queue for ingest
# On save_recipe("user-789", ...):
# Check: "user-789" in connected_users → no → passthrough
```

---

## Architecture: What Gets Built

### Enzyme Hosted API (extends Node server)

```
POST /v1/tenants                              Auto-register tenant
PUT  /v1/tenants/:id                          Update config
POST /v1/tenants/:id/ingest                   Queue entries for a user
GET  /v1/tenants/:id/connected-users          List connected user IDs (for SDK cache)
POST /v1/tenants/:id/fetch-callback           Register callback URL for @fetch
POST /v1/tenants/:id/webhooks/data-updated    Notify data change
GET  /v1/tenants/:id/users/:uid/status        User connection status
DELETE /v1/tenants/:id/users/:uid             Delete user + data
```

### OAuth Provider

```
GET  /.well-known/protected-resource/{tenant}  RFC 9728 discovery
GET  /oauth/{tenant}/authorize                  Consent screen
POST /oauth/{tenant}/token                      Token exchange + refresh
```

### Connection Pipeline (triggered on OAuth success)

```
OAuth success
  → resolve tenant + user
  → create Turso child DB
  → call app's @fetch function via callback URL
  → receive entries
  → run pipeline: ingest → embed → cluster → catalyze
  → status: 'ready'
  → Claude's next tool call gets real data
```

### Multi-tenant MCP (enzyme-search)

```
POST /{tenant}/mcp
```

Token → tenant + user → Turso DB → ContextSearch → response.

---

## Metadata Schema

```sql
CREATE TABLE tenants (
    id               TEXT PRIMARY KEY,
    display_name     TEXT NOT NULL,
    logo_url         TEXT,
    description      TEXT,
    oauth_mode       TEXT NOT NULL DEFAULT 'enzyme_managed',
    oauth_config     TEXT,               -- JSON (delegated mode)
    tool_config      TEXT,               -- JSON: custom tool names/descriptions
    entity_types     TEXT,               -- JSON array: ['recipe', 'journal_entry']
    data_schema      TEXT,               -- JSON: inferred from decorator returns
    callback_url     TEXT,               -- URL for @fetch calls
    schema_db_name   TEXT,               -- Turso parent schema database
    refresh_interval INTEGER DEFAULT 86400,
    api_key_hash     TEXT NOT NULL UNIQUE,
    created_at       INTEGER NOT NULL
);

CREATE TABLE tenant_users (
    tenant_id        TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    email            TEXT,
    status           TEXT NOT NULL DEFAULT 'discovered',
    db_name          TEXT,               -- NULL until connected
    db_url           TEXT,               -- NULL until connected
    doc_count        INTEGER DEFAULT 0,
    catalyst_count   INTEGER DEFAULT 0,
    last_indexed     INTEGER,
    connected_at     INTEGER,
    created_at       INTEGER NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);
```

---

## Phase Plan

| Phase | Scope | Deliverable |
|-------|-------|-------------|
| **1: Decorator SDK** | `@enzyme.on_save`, `@enzyme.fetch`, `EnzymeHosted` client, local preview/sample | App integration in 3 functions |
| **2: Tenant API** | Auto-registration, callback URL, connected-users endpoint, ingest queue | Backend for decorator calls |
| **3: OAuth Provider** | RFC 9728, enzyme-managed consent, PKCE, token issuance | Users can connect in Claude |
| **4: Connection Pipeline** | On-connect: create DB, call fetch, index, mark ready | First query works |
| **5: Multi-tenant MCP** | Per-tenant routing, token resolution, ContextSearch | Claude queries user data |
| **6: Incremental Ingest** | `@on_save` queuing, async processing, data freshness | Live updates |
| **7: Delegated OAuth** | Proxy to app's OAuth, JWKS validation | Apps control auth |
| **8: Directory Tooling** | `prepare_submission()`, auto-generated docs/tools | Streamlined listing |
