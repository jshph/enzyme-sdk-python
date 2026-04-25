# Enzyme SDK

Enzyme pre-indexes user content so agents can access patterns the user never articulated. You send structured data in, enzyme generates thematic questions from it at build time, and agents search against those questions instead of raw embeddings.

## What the agent produces

A cooking app user has 407 saved recipes with annotations spanning 6 years. The agent gets asked: *"I'm hosting a dinner party, a couple friends are vegetarian. What should I make?"*

> You've noted your **Mushroom Hash with Black Rice** is "now in my repertoire" — you make it with the black rice you get from Costco. It scales easily for 6.
>
> For a side, your **Orecchiette with Swiss Chard and Feta** — you described it as "very pretty, easy, and delicious" and liked that "the cheese stayed intact."
>
> A warning from your own notes: your **Red Cabbage and Black Rice** lacked flavor and the cabbage got "lost." Season the hash aggressively.

This isn't template filling. The agent surfaced the black rice recipe because enzyme's index noticed this user keeps returning to black rice across multiple dishes. The warning came from a *different* recipe — enzyme connected the failure to the recommendation because they share a cluster.

## How it works

Enzyme groups your data into clusters and generates **catalysts** — thematic questions — for each cluster. When the agent queries, it matches against those questions, not document text.

```
Agent query:     "vegetarian dinner ideas"

Catalyst match:  "What does the commitment to vegetarianism cost when
                  Italian sausage and broccoli rabe are on the menu?"
```

The agent's query is generic. The catalyst is specific to *this* user — someone who saves sausage dishes despite cooking vegetarian most of the time. The personalization happened at index time.

### How clusters form

Enzyme can create automatic clusters from entry bodies before ingest. These become readable automatic tags like `auto-cluster-black-rice`. Catalysts are generated from the user's own entries in each cluster; the tag keywords are only a weak display hint.

You can still attach your own labels when you have strong domain structure. For uncurated data, build automatic clusters first and ingest the enriched entries.

## Get started

Here's the full integration we tested — loading 407 recipe comments from a CSV, ingesting them, and wiring up an agent.

### 1. Load and ingest

Your data comes from a database or CSV:

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
# Embeddings run locally (no API). Catalysts need an LLM — set OPENAI_API_KEY.
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
