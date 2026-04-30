# Enzyme SDK

Enzyme automatically manages your app's user conversations, collections, and agent traces, enabling you to quickly experiment with context-efficient and responsive agent-collaborative UX and MCP integrations.

## Example

A cooking app user has 407 saved recipes with annotations spanning 6 years. The agent gets asked: *"I'm hosting a dinner party, a couple friends are vegetarian. What should I make?"*

> You've noted your **Mushroom Hash with Black Rice** is "now in my repertoire"
> and that it scales well. For a side, your **Orecchiette with Swiss Chard and
> Feta** was "very pretty, easy, and delicious."
>
> A warning from your own notes: your **Red Cabbage and Black Rice** lacked
> flavor and the cabbage got "lost." Season the hash aggressively.

The user never asked for "black rice." Enzyme surfaced it because the index had already noticed that pattern across the user's recipe comments.

## Install

```bash
pip install -e .
```

For local development with the examples:

```bash
pip install -e ".[dev,cluster,examples]"
```

## Example: Build an MCP Connector

This example builds from a sample dataset of NYT Cooking comments. Rows have `user_key`, `user_id`, `recipe_name`, `comment`, and `date`.

```python
from dataclasses import dataclass
from typing import Iterable

from enzyme_sdk import Activity, CatalystProfile, EnzymeConnector, enzyme


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

@dataclass
class RecipeComment:
    pass

@dataclass
class ObservedPreference:
    pass

connector = EnzymeConnector(
    app_id="nyt-cooking",
    display_name="NYT Cooking",
    content_label="cooking notes",
    catalyze_tool="catalyze_cooking_notes",
    catalyze_description=(
        "Search this user's cooking history by concept: saved recipes, "
        "annotations, substitutions, outcomes, and personal notes. Results "
        "include the source notes plus the catalysts that explain why they matched."
    ),
    profile_tool="get_cooking_profile",
    profile_description=(
        "Inspect this user's cooking profile: recurring ingredients, techniques, "
        "cuisines, constraints, and the catalysts that characterize each area."
    ),
    collections=[RecipeComment, ObservedPreference],
    catalyst_profiles={
        RecipeComment: CatalystProfile.PREFERENCE_EVIDENCE,
        ObservedPreference: CatalystProfile.PREFERENCE_EVIDENCE,
    },
)

@enzyme.hydrate(connector)
def get_activity(user_id: str) -> Iterable[CookingEvent]:
    return db.load_recipe_activity(user_id)

@enzyme.transform(connector)
def activity_to_enzyme(event: CookingEvent) -> Activity:
    return Activity(
        title=event.recipe_name,
        content=event.comment,
        created_at=event.date,
        source_id=event.id,
        collections=[RecipeComment],
        metadata={
            "activity_type": event.kind,
            "labels": [*event.source_tags, *event.auto_tags],
        },
    )

@enzyme.on_save(connector)
def save_activity(user_id: str, event: CookingEvent) -> CookingEvent:
    return db.save(event)
```

### Field Mapping

| Field | Purpose |
| --- | --- |
| `@enzyme.transform` | Converts your app-native object into an `Activity` ingest payload. Hydrate and save hooks both use it. |
| `collections` | Maps one item to one or more per-user activity classes. Enzyme stores them as stable collection ids, such as `recipe-comment`, `observed-preference`, `message`, or `folder-inbox`. CLI-backed ingest also associates these ids with the document as folder-style catalyst entities. |
| `catalyst_profiles` | Optionally tells catalyst generation how to treat a collection, for example preference evidence, operational traces, or decision traces. |
| `source_id` | Tells your app how to hydrate an activity back into its own UX. |
| `content` + `metadata` | Body text plus small structured context; the SDK folds both into the string Enzyme ingests. |
| `created_at` | Enables recency-aware ranking and catalyst context. |

If your app has distinct activity types, such as recipe comments, saved
recipes, agent-observed preferences, messages, projects, or artifacts, model
them as small typed collection classes and return those classes from
`Activity.collections`. If an item belongs to multiple activity collections,
return multiple classes; Enzyme can route through several catalysts and converge
on the same chunk or document. `@enzyme.collection` remains available for older
integrations that only need collection labeling, but new connectors should
prefer one `@enzyme.transform`.

The connector mapping becomes the structured ingest payload that the Enzyme
binary consumes. `tags` become tag entities. `links` become link entities when
provided through direct ingest or rendered markdown. `collection`,
`collections`, and `folder` become folder entities in the Rust index,
equivalent to folders derived from a markdown file path. For connector-backed
local runs, the SDK also writes configured collection entities and catalyst
profiles into Enzyme's local config so the CLI can generate catalysts around
the same activity boundaries your app uses. SDK integrators should know that
collection ids are the bridge into Enzyme's tag/folder/link entity model.

### Serve MCP

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
    print(result.source_id, result.title)

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
python examples/agent_test.py
```

`examples/nyt_sample_comments.json` includes comments for three sample users.
`agent_test.py` hydrates the decorated connector from `examples/run_mcp_server.py`,
prints the typed activity collection counts, builds a temporary local index,
and then runs an agent over that user's indexed recipe notes.

Use `OPENAI_MODEL` to change models. Set `OPENAI_BASE_URL` for a compatible
provider.

```bash
ENZYME_TEST_USER=dimmerswitch python examples/agent_test.py
```
