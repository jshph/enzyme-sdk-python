# Enzyme SDK

Build Claude/MCP connectors for apps where user taste accumulates: saved recipes, playlists, journal entries, reading highlights, bookmarks, places, boards, reviews, and annotations.

Enzyme pre-indexes each user's content so agents can work with the patterns behind the data, not just matching rows. You send structured data in, Enzyme generates thematic questions from it at build time, and agents search against those questions instead of raw document embeddings.

## What the agent produces

A cooking app user has 407 saved recipes with annotations spanning 6 years. The agent gets asked: *"I'm hosting a dinner party, a couple friends are vegetarian. What should I make?"*

> You've noted your **Mushroom Hash with Black Rice** is "now in my repertoire" — you make it with the black rice you get from Costco. It scales easily for 6.
>
> For a side, your **Orecchiette with Swiss Chard and Feta** — you described it as "very pretty, easy, and delicious" and liked that "the cheese stayed intact."
>
> A warning from your own notes: your **Red Cabbage and Black Rice** lacked flavor and the cabbage got "lost." Season the hash aggressively.

This isn't template filling. The agent surfaced the black rice recipe because Enzyme's index noticed this user keeps returning to black rice across multiple dishes. The warning came from a *different* recipe — Enzyme connected the failure to the recommendation because they share a cluster.

## The connector shape

Point Enzyme at the user's rows. Claude gets two user-scoped tools, with names and descriptions you control:

1. A profile tool — see the user's clusters and the catalysts that describe each area.
2. A search tool — search the user's history by concept and return the source entries plus the catalysts that routed them.

The result is a Claude connector that understands the user's accumulated taste on day one. A new conversation can start from "what should I cook?", "where should I travel?", or "what should I listen to?" without making the user re-explain years of choices.

## How it works

Enzyme groups your data into clusters and generates **catalysts** — inspectable thematic questions — for each cluster. At query time, the agent matches against those questions, then Enzyme returns the source entries attached to the best-matching catalysts.

```
Agent query:     "vegetarian dinner ideas"

Catalyst match:  "What does the commitment to vegetarianism cost when
                  Italian sausage and broccoli rabe are on the menu?"
```

The agent's query is generic. The catalyst is specific to *this* user — someone who saves sausage dishes despite cooking vegetarian most of the time. The personalization happened at index time.

## What is a catalyst?

A catalyst is an inspectable question Enzyme generated from one user's data.

You do not write catalysts. They are not tags or hidden prompts. Enzyme creates them while building the index, embeds them, and links each one back to the entries that produced it.

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

The agent can quote the user's own notes and explain why those notes surfaced.

## Two integration layers

Use `EnzymeConnector` when you want to expose user-scoped app data to an agent through MCP. It owns user hydration, save hooks, MCP tool names and descriptions, per-user indexes, and the local dev server.

Use `EnzymeClient` when you want direct control over vaults: ingest entries, build clusters, refresh the index, call `catalyze()`, and call `petri()` yourself. It has no MCP or app lifecycle opinions.

## Serve as an MCP connector

Configure the MCP tool surface on the connector, then use decorators for the data contract. Enzyme handles hydration, clustering, catalyst generation, and MCP serving. Claude gets catalyst-routed search: "vegetarian dinner for friends" can find the black-rice pattern, not just recipes tagged vegetarian.

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
    catalyze_description=(
        "Search this user's cooking history — recipe annotations, substitutions, "
        "results, and personal notes. Broad queries work well. Results include "
        "the thematic signals that connected the query to the content."
    ),
    profile_tool="get_cooking_profile",
    profile_description=(
        "See what this user's cooking history reveals — recurring ingredients, "
        "techniques they've adopted or abandoned, and the thematic questions that "
        "characterize each area."
    ),
)

@enzyme.hydrate(connector)
def get_activity(user_id: str) -> list[CookingEvent]:
    return db.load_recipe_activity(user_id)

@enzyme.collection(connector)
def activity_collection(event: CookingEvent) -> str:
    if event.source_tags:
        return f"recipe/{event.source_tags[0]}"
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
    return db.save(event)  # return value unchanged

connector.serve(port=9460, init_users=["user-1", "user-2"])
```

`serve()` hydrates each user's data, auto-clusters it, builds the catalyst index, and starts a JSON-RPC 2.0 MCP server. Pass `--ngrok` to expose it for Claude:

```bash
python examples/run_mcp_server.py --ngrok
```

`examples/dishgen_app.py` shows mounting MCP alongside a CRUD API on the same FastAPI server.

API keys for the connector path: sign in at [enzyme.garden](https://enzyme.garden/login), create a key at `/settings`, set `ENZYME_API_KEY`. Catalyst generation uses Enzyme's hosted generation path — no OpenAI key needed for this connector flow.

## Mapping app data

`EnzymeConnector` starts from your app's source schema. The important part is
making the dated item shape explicit, then telling Enzyme which fields define
collections and which fields define entities inside those collections.

Using the NYT Cooking sample as the throughline, the source data starts as
dated recipe comments. The raw export has no tags, so the app derives
`auto_tags` with body clustering before handing rows to Enzyme:

```python
from dataclasses import dataclass

@dataclass
class CookingEvent:
    id: str
    user_id: str
    kind: str              # "recipe_comment", later maybe "saved_recipe"
    recipe_name: str
    comment: str
    date: str
    source_tags: list[str] # if your app has them; the bundled sample does not
    auto_tags: list[str]
```

The connector API makes collection mapping explicit. The `.collection` hook is
the declarative bridge from your item schema to Enzyme's per-user ingest and
cache partitions:

```python
from enzyme_sdk import EnzymeConnector, enzyme

connector = EnzymeConnector(
    app_id="nyt-cooking",
    display_name="NYT Cooking Notes",
    content_label="cooking activity",
)

@enzyme.hydrate(connector)
def get_activity(user_id: str) -> list[CookingEvent]:
    return db.load_recipe_activity(user_id)

@enzyme.collection(connector)
def activity_collection(event: CookingEvent) -> str:
    if event.source_tags:
        return f"recipe/{event.source_tags[0]}"
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

The mapping has three jobs:

- `@enzyme.collection` maps each dated source item to a per-user collection id,
  such as `recipe/main-dishes`, `recipe/desserts`, `saved_recipe`, `message`,
  `artifact`, or `folder/inbox`. This is the ingest, refresh, and cache
  boundary.
- `tags=lambda event: [*event.source_tags, *event.auto_tags]` creates entities
  inside those collections, so catalysts can form around recipes, ingredients,
  people, folders, labels, projects, or automatic cluster labels.
- `primitive="kind"` and `source_id="id"` tell your app how to hydrate the
  result back into its own UX.

Source tags are optional but valuable. The bundled NYT sample does not include
recipe tags, only `user_key`, `user_id`, `recipe_name`, `comment`, and `date`,
so the example falls back to `event.kind` as the collection delimiter and uses
body clustering to create `auto_tags`. If your product already has stable tags,
folders, channels, projects, or recipe categories, using one of those as the
collection delimiter lets Enzyme refresh and cache narrower partitions, compare
activity within meaningful product areas, and still search across the full
user scope at query time.

The collection hook can return a field directly, normalize a field, or combine
multiple fields:

```python
@enzyme.collection(connector)
def email_collection(message: EmailMessage) -> str:
    return message.folder.lower()  # inbox, sent, archive
```

If the same source field is also useful as a retrieval entity, pass it through
`tags` too. For example, an email `folder` can define the collection boundary,
while `labels`, `people`, and `project_id` become entities catalysts form
around.

Hosted search uses the same connector semantics. The hosted service composes
the user's collections into one app/user scope, and callers enter through the
connector instead of constructing a separate hosted client abstraction:

```python
scope = connector.hosted("user-123")
response = scope.catalyze("quick weeknight dinners with ginger", limit=8)

for result in response.results:
    # Your app owns hydration and UX.
    print(result.primitive, result.source_id, result.title)

overview = scope.petri(top=12)
status = scope.status()
```

`catalyze()` still searches the full app/user scope. It does not take a public
collection selector, and normal results do not expose storage collection ids.
Results should be hydrated through app fields:

```text
primitive = "message" | "thread" | "artifact" | "recipe_note"
source_id = your app's stable row/document id
```

Use `status()` for internal health and debugging; it can show the participating
collections, counts, and cache epochs. Treat those as ingest/cache partitions,
not as user-facing relevance semantics.

In the NYT Cooking example, the raw sample data has no source tags. It has
`user_key`, `user_id`, `recipe_name`, `comment`, and `date`. The example derives
`auto_tags` from body clustering, then ingests a user's dated recipe comments
with those labels so catalysts have entities to form around.

## Direct integration

For more control — loading data from CSVs, managing clustering yourself, wiring tools into your own agent harness — use `EnzymeClient` directly.

### How clusters form

Enzyme can create automatic clusters from entry bodies before ingest. These become readable automatic tags like `auto-cluster-black-rice`. Catalysts are generated from the user's own entries in each cluster; the tag keywords are only a weak display hint.

You can still attach your own labels when you have strong domain structure. For uncurated data, build automatic clusters first and ingest the enriched entries.

### 1. Load and ingest

```python
import csv
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()  # ~11MB binary + ~52MB embedding model

with open("comments.csv") as f:
    all_recipes = list(csv.DictReader(f))

def recipe_text(recipe):
    return f"{recipe['recipe_name']}\n\n{recipe['comment']}"

# Build reusable automatic labels across all users.
cluster_index = client.build_entry_cluster_index(
    all_recipes,
    text=recipe_text,
    # granularity="fine",  # broader coverage for sparse corpora
)

# Ingest one user's entries. Catalysts are generated from this user's corpus.
user_recipes = [
    recipe for recipe in all_recipes
    if recipe["user_id"] == "user-123"
]
assigned = cluster_index.assign(
    user_recipes,
    text=recipe_text,
    target_field="auto_tags",
)

client.ingest(
    collection="user-123",
    entries=[
        {
            "title": recipe["recipe_name"].replace("-", " ").title(),
            "notes": recipe["comment"],
            "tags": recipe.get("auto_tags", []),
            # "tags": recipe["source_tags"],  # use source tags without auto clustering
            # "tags": recipe.get("source_tags", []) + recipe.get("auto_tags", []),
            "created_at": recipe["date"],
        }
        for recipe in assigned.entries
    ],
)
```

Single-user app:

```python
assigned = client.cluster_entries(
    user_recipes,
    text=recipe_text,
    target_field="auto_tags",
)
```

### 2. Build the index

```python
# Embeddings run locally (no API). In direct mode, catalyst generation uses
# your configured LLM provider — set OPENAI_API_KEY or OPENAI_BASE_URL.
# ~6s for 400 entries.
client.init(collection="user-123")
```

### 3. Wire up the agent

```python
from agents import Agent, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel

enzyme = EnzymeClient()

@function_tool(description_override=EnzymeClient.tool_description("catalyze"))
def explore(query: str) -> str:
    return enzyme.catalyze(query, collection="user-123").render_to_prompt()

@function_tool(description_override=EnzymeClient.tool_description("petri"))
def get_overview() -> str:
    return enzyme.petri(collection="user-123").render_to_prompt()

agent = Agent(
    name="Cooking assistant",
    model=OpenAIChatCompletionsModel(model="...", openai_client=your_client),
    tools=[explore, get_overview],
    instructions="""\
You are a cooking assistant that knows this user's actual cooking history.

You have two tools. Use them sparingly:
1. get_overview — call ONCE to see the user's clusters and catalysts.
2. explore — call ONCE to search their history by concept.

Do not call tools more than twice total.

Catalysts in the results explain *why* content surfaced — they encode
patterns the user never articulated. Use them to frame your response.

Quote the user's own words from their notes. Don't paraphrase.
Synthesize across results — don't enumerate them.
""",
)
```

The system prompt matters. Without the tool-call cap, agents make 5-7 redundant calls. Without explaining catalysts, agents treat results as generic RAG. Without "quote their words," agents paraphrase away the personal signal.

## Running the full example

```bash
pip install -e ".[dev,cluster]" openai-agents
python examples/prepare_nyt_data.py es    # writes nyt_es_data.json to the system temp dir
python examples/agent_test.py             # set OPENAI_API_KEY in .env
```

`examples/nyt_sample_comments.json` includes all comments for three sample users.
`prepare_nyt_data.py` prepares all three users for cluster labels plus one user
for ingest. `agent_test.py` builds automatic cluster tags from all three users
and ingests only the selected user's entries.

Use `OPENAI_MODEL` to change models. Set `OPENAI_BASE_URL` for a compatible
provider.

Try a different user to see how the same prompt produces completely different output:

```bash
python examples/prepare_nyt_data.py dimmerswitch
ENZYME_TEST_USER=dimmerswitch python examples/agent_test.py
```
