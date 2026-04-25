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

Enzyme clusters content using labels you attach to entries. In the cooking example, entries get labels like `miso`, `italian`, `braising` — derived from recipe categories and the user's own comment text. Miso salmon, miso banana bread, and miso eggplant land in the same cluster. Enzyme sees the user keeps substituting miso for dairy fats and generates a question about that pattern.

The labels don't need to be perfect or hand-curated. Derive them from whatever you have: product categories, ingredient lists, keywords in user text. Without any labels, enzyme still clusters by folder and content similarity — but labeling tells it which groupings matter for your domain.

## Get started

Here's the full integration we tested — loading 407 recipe comments from a CSV, ingesting them, and wiring up an agent.

### 1. Load and ingest

Your data comes from a database or CSV. Write an adapter function that maps each row to an enzyme entry:

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()  # ~11MB binary + ~52MB embedding model

# The adapter — maps your row to an enzyme entry.
# This is the part you change for your dataset.
LABEL_KEYWORDS = {
    "chicken": "chicken", "salmon": "salmon", "pasta": "italian",
    "miso": "japanese", "roast": "roasting", "stew": "stew",
    "parmesan": "cheese", "lemon": "citrus", "chili": "heat",
    "cast iron": "cast-iron", "leftover": "leftovers",
    # ... scan recipe name + comment text for domain-relevant keywords
}

def row_to_entry(row):
    text = (row["recipe_name"] + " " + row["comment"]).lower()
    labels = sorted({tag for kw, tag in LABEL_KEYWORDS.items() if kw in text})
    return {
        "title": row["recipe_name"].replace("-", " ").title(),
        "notes": row["comment"],       # the user's voice — this is the signal
        "tags": labels,                 # how enzyme clusters
        "folder": "saves",
        "created_at": row["date"],      # ISO date or epoch ms
    }

# Batch load from your data source
import csv
with open("comments.csv") as f:
    entries = [row_to_entry(row) for row in csv.DictReader(f)]

client.ingest(collection="user-123", entries=entries)
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
pip install -e ".[dev]" openai-agents
python examples/prepare_nyt_data.py es    # extracts 407 entries from NYT dataset
python examples/agent_test.py             # runs agent (set OPENROUTER_API_KEY in .env)
```

`prepare_nyt_data.py` shows the full label derivation pipeline — scanning recipe names AND comment text for ingredients, cuisines, techniques, and cooking behaviors. `agent_test.py` is the complete working integration.

Try a different user to see how the same prompt produces completely different output:

```bash
python examples/prepare_nyt_data.py dimmerswitch
ENZYME_TEST_USER=dimmerswitch python examples/agent_test.py
```
