# Enzyme SDK

Give agents structural understanding of a user's accumulated choices — not just retrieval, but the patterns behind what they've saved, annotated, and returned to.

## Why this exists

An agent asks a generic question. Enzyme routes it through precomputed thematic questions (catalysts) generated from the user's own content. The catalysts do the personalization — the agent doesn't need to write good queries.

```
Agent query:     "vegetarian dinner ideas"

Catalyst match:  "What does the commitment to vegetarianism cost when
                  Italian sausage and broccoli rabe are on the menu?"

→ Returns this user's saved Broccoli Rabe Lasagna with their note:
  "David Tanis' rapini recipes are genius!"
```

The agent wrote a query any user would write. The catalyst is specific to *this* user — someone who keeps saving sausage dishes despite cooking vegetarian most of the time. Enzyme found that tension at index time, not query time.

## Get started

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()

# 1. Ingest user data directly — no files needed
client.ingest(collection="user-123", entries=[
    {
        "title": "Miso Glazed Salmon",
        "notes": "Subbed white miso for butter. Way better.",
        "tags": ["seafood", "japanese", "miso"],
        "folder": "saves",
        "created_at": "2025-06-15",
    },
    # ...
])

# 2. Build the index
client.init(collection="user-123")

# 3. Search by concept
results = client.catalyze("comfort food", collection="user-123")
print(results.render_to_prompt())  # structured text ready for an LLM
```

## Agent integration

```python
from agents import Agent, function_tool
from enzyme_sdk import EnzymeClient

enzyme = EnzymeClient()

@function_tool(description_override=EnzymeClient.tool_description("catalyze"))
def explore(query: str) -> str:
    return enzyme.catalyze(query, collection=user_id).render_to_prompt()

@function_tool(description_override=EnzymeClient.tool_description("petri"))
def get_overview() -> str:
    return enzyme.petri(collection=user_id).render_to_prompt()

agent = Agent(
    name="Cooking assistant",
    tools=[explore, get_overview],
    instructions="...",  # see examples/agent_test.py
)
```

Two tools: `petri` at session start (what does the index know), `catalyze` mid-conversation (search by concept). Both return structured text with matched documents, contributing catalysts, and the user's own annotations.

## What you build vs. what enzyme handles

**You build:** the data pipeline (entries with tags) and the system prompt (how the agent reads enzyme output).

**Enzyme handles:** embedding, catalyst generation, query routing, personalization. No query rewriting, no preference extraction, no relevance tuning.

## Data shape

Tags are the main design decision. They become entities → entities get catalysts → catalysts are retrieval paths.

Good tags cluster behavior that recurs: ingredients (`miso`, `tahini`), cuisines (`italian`), techniques (`braising`), habits (`weeknight`, `make-ahead`). Derive them from your metadata + user text. See `examples/prepare_nyt_data.py` for a working pipeline.

Target: 2-3 tags per entry, 15+ tags with 10+ entries each, <10% untagged entries. 150-300 entries is enough for useful catalysts.

**What goes wrong:** sparse tags → generic catalysts. One catch-all tag covering everything → catalysts that distinguish nothing. Agent calling tools 5+ times → wasted tokens. Fix that last one with an explicit cap in the system prompt.

## Example

`examples/agent_test.py` — ingests 318 NYT Cooking recipe comments from one user, builds the index (60 catalysts across 20 entities), then runs an agent conversation via OpenRouter. The agent makes 2 tool calls and produces recommendations grounded in the user's actual cooking history.

```bash
pip install -e ".[dev]" openai-agents
python examples/prepare_nyt_data.py   # needs kaggle dataset
python examples/agent_test.py         # needs OPENROUTER_API_KEY
```
