# Enzyme SDK

Build Claude/MCP connectors for apps where user taste accumulates: saved
recipes, playlists, journal entries, reading highlights, bookmarks, places,
boards, reviews, and annotations.

Enzyme pre-indexes each user's content so agents can retrieve patterns, not
just matching rows. You send structured data in; Enzyme generates inspectable
thematic questions, called catalysts, and uses them to route search back to the
source entries.

## What It Enables

A cooking app user has 407 saved recipes with annotations spanning 6 years. The
agent gets asked: *"I'm hosting a dinner party, a couple friends are vegetarian.
What should I make?"*

> You've noted your **Mushroom Hash with Black Rice** is "now in my repertoire"
> and that it scales well. For a side, your **Orecchiette with Swiss Chard and
> Feta** was "very pretty, easy, and delicious."
>
> A warning from your own notes: your **Red Cabbage and Black Rice** lacked
> flavor and the cabbage got "lost." Season the hash aggressively.

The user never asked for "black rice." Enzyme surfaced it because the index had
already noticed that pattern across the user's notes.

## Install

```bash
pip install enzyme-sdk
```

For local development with the examples:

```bash
pip install -e ".[dev,cluster]" openai-agents
```

## Choose An API

| Use | API |
| --- | --- |
| Build an MCP connector over app data | `EnzymeConnector` |
| Ingest/search directly from Python | `EnzymeClient` |
| Query hosted app/user search | `connector.hosted(user_id)` |

`EnzymeConnector` owns user hydration, save hooks, MCP tool names, per-user
indexes, and the local dev server. `EnzymeClient` is the lower-level API for
manual ingest, refresh, `catalyze()`, and `petri()`.

## Build A Connector

This example uses the bundled NYT Cooking sample as the throughline. The sample
rows have `user_key`, `user_id`, `recipe_name`, `comment`, and `date`. They do
not include source recipe tags, so the example falls back to `event.kind` for
collection mapping and uses `auto_tags` from clustering.

```python
from dataclasses import dataclass

from enzyme_sdk import EnzymeConnector, enzyme

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
    app_id="nyt-cooking",
    display_name="NYT Cooking",
    content_label="cooking notes",
    catalyze_tool="catalyze_cooking_notes",
    profile_tool="get_cooking_profile",
)

@enzyme.hydrate(connector)
def get_activity(user_id: str) -> list[CookingEvent]:
    return db.load_recipe_activity(user_id)

@enzyme.collection(connector)
def activity_collection(event: CookingEvent) -> str | list[str]:
    if event.source_tags:
        return [f"recipe/{tag}" for tag in event.source_tags]
    return event.kind

@enzyme.on_save(
    connector,
    title="recipe_name",
    content="comment",
    created_at="date",
    tags=lambda event: [*event.source_tags, *event.auto_tags],
    primitive="kind",
    source_id="id",
)
def save_activity(user_id: str, event: CookingEvent) -> CookingEvent:
    return db.save(event)
```

### Field Mapping

| Field | Purpose |
| --- | --- |
| `@enzyme.collection` | Maps one item to one or more per-user collection labels, such as `recipe/main-dishes`, `message`, or `folder/inbox`. CLI-backed ingest also associates these labels with the document as folder-style catalyst entities. |
| `tags` | Adds catalyst entities inside those collections. Use source tags, labels, people, projects, folders, and automatic cluster labels here. |
| `primitive` + `source_id` | Tells your app how to hydrate a result back into its own UX. |
| `created_at` | Enables recency-aware ranking and catalyst context. |

If your app has stable folders, channels, projects, or recipe categories, return
them from `@enzyme.collection`. If an item belongs to multiple source tags,
return multiple labels; Enzyme can route through several catalysts and converge
on the same chunk or document.

## Serve MCP

```python
connector.serve(port=9460, init_users=["user-1", "user-2"])
```

`serve()` hydrates each user, builds the catalyst index, and starts a JSON-RPC
2.0 MCP server.

```bash
python examples/run_mcp_server.py --ngrok
```

`examples/dishgen_app.py` shows mounting MCP alongside a CRUD API on the same
FastAPI server.

API keys for the connector path: sign in at
[enzyme.garden](https://enzyme.garden/login), create a key at `/settings`, and
set `ENZYME_API_KEY`. Catalyst generation uses Enzyme's hosted generation path,
so no OpenAI key is needed for this connector flow.

## Query Hosted Search

Hosted search uses the same connector semantics. The service composes the
user's collections into one app/user scope.

```python
scope = connector.hosted("user-123")
response = scope.catalyze("quick weeknight dinners with ginger", limit=8)

for result in response.results:
    print(result.primitive, result.source_id, result.title)

overview = scope.petri(top=12)
status = scope.status()
```

`catalyze()` searches the full app/user scope. It does not take a public
collection selector, and normal results do not expose storage collection ids.
Use `status()` for internal health, collection counts, and cache epochs.

## Use The Direct Client

Use `EnzymeClient` when you want lower-level control over ingest, clustering,
and agent wiring.

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()

entries = [
    {
        "title": recipe["recipe_name"].replace("-", " ").title(),
        "notes": recipe["comment"],
        "tags": recipe.get("source_tags", []) + recipe.get("auto_tags", []),
        "collections": [f"recipe/{tag}" for tag in recipe.get("source_tags", [])],
        "created_at": recipe["date"],
    }
    for recipe in user_recipes
]

client.ingest(collection="user-123", entries=entries)
client.init(collection="user-123")

results = client.catalyze("vegetarian dinner ideas", collection="user-123")
overview = client.petri(collection="user-123")
```

Automatic clusters are optional. For uncurated data, build cluster labels first
and include them in `tags`:

```python
cluster_index = client.build_entry_cluster_index(all_recipes, text=recipe_text)
assigned = cluster_index.assign(user_recipes, text=recipe_text, target_field="auto_tags")
```

## What Comes Back

`catalyze()` returns the matched catalysts and the entries they routed:

```text
Query: "vegetarian dinner ideas"

Routing signals:
- auto-cluster-black-rice: What makes black rice the user's reliable base
  when cooking for groups? (routed 2 results)

Matched documents:
1. Mushroom Hash with Black Rice
   "now in my repertoire"
2. Red Cabbage and Black Rice
   "the cabbage got lost"
```

Agents should use catalysts as evidence for why something surfaced, then quote
the user's own source text.

## Run The Example

```bash
python examples/prepare_nyt_data.py es
python examples/agent_test.py
```

`examples/nyt_sample_comments.json` includes comments for three sample users.
`prepare_nyt_data.py` prepares cluster labels across all three users and writes
one selected user's ingest file. `agent_test.py` builds an agent over that
user's indexed recipe notes.

Use `OPENAI_MODEL` to change models. Set `OPENAI_BASE_URL` for a compatible
provider.

```bash
python examples/prepare_nyt_data.py dimmerswitch
ENZYME_TEST_USER=dimmerswitch python examples/agent_test.py
```
