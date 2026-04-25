# Enzyme SDK

Python client for [Enzyme](https://enzyme.garden). Gives agents structural understanding of a user's accumulated choices — not just retrieval, but the patterns behind what they've saved, annotated, and returned to.

## Why this exists

Standard RAG retrieves documents that match a query. Enzyme retrieves documents that match a *pattern the user never articulated*.

An agent asks a generic question. Enzyme routes it through precomputed thematic questions (catalysts) that were generated from the user's content. The catalysts do the personalization work — the agent doesn't have to write good queries.

```
Agent query (generic):
  "vegetarian dinner ideas"

Catalyst that matched (generated from this user's 318 saved recipes):
  "What does the commitment to vegetarianism cost when Italian sausage
   and broccoli rabe are on the menu?"

  → routed to: Broccoli Rabe Lasagna, Sausage with Peppers and Onions
  → user's note: "David Tanis' rapini recipes are genius!"
```

The agent wrote a query any user would write. The catalyst is specific to *this* user — someone who keeps saving sausage dishes despite cooking vegetarian most of the time. That tension is the signal. Enzyme found it at index time, not query time.

**What you don't build:** query rewriting, user-preference extraction, embedding-time personalization, or prompt engineering to make retrieval feel personal. Enzyme's compile step handles that.

**What you do build:** the data pipeline (getting user content into enzyme's format) and the system prompt (telling the agent how to read enzyme's output).

## Quick start

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()

# 1. Ingest user data (from your app's DB, not files)
client.ingest(collection="user-123", entries=[
    {
        "title": "Miso Glazed Salmon",
        "content": "Pan-seared salmon with white miso glaze",
        "notes": "Subbed white miso for butter. Way better.",
        "tags": ["seafood", "japanese", "miso"],
        "folder": "saves",
        "created_at": "2025-06-15",
    },
    # ...
])

# 2. Build the index (embeddings + catalysts)
client.init(collection="user-123")

# 3. Query — agent calls this mid-conversation
results = client.catalyze("comfort food for a cold night", collection="user-123")
print(results.render_to_prompt())  # structured text ready for an LLM

# 4. Overview — agent calls this at session start
overview = client.petri(collection="user-123")
print(overview.render_to_prompt())
```

`--collection` resolves to `~/.enzyme-sdk/collections/<id>/.enzyme/enzyme.db`. No vault directory or markdown files needed.

## How agents use this

Two tools. Call the first at session start, the second when the user asks something.

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
    instructions=SYSTEM_PROMPT,
)
```

The agent gets back structured text with:
- **Matched documents** — the user's content with their annotations
- **Contributing catalysts** — the thematic questions that routed results, explaining *why* this content surfaced
- **Presentation guidance** — per-catalyst framing instructions (when using the explore register)

### The agent doesn't need to write good queries

This is the key thing. An LLM will write queries like `"vegetarian dinner recipes with miso or tahini"`. That's a keyword search dressed up as natural language. It would work fine with any embedding search.

But enzyme routes that query through catalysts like:

> "Where does the simplicity of 'vegetarian' pull against the complexity of balancing flavors in dishes like Mushroom Spinach Soup?"

That catalyst exists because this user has 77 entries tagged vegetarian and their comments reveal a pattern of reaching for complex flavor despite choosing simple vegetarian formats. The generic query activates a personalized retrieval path. The agent gets results that reflect how this person actually cooks, not just what matches the words.

### System prompt: what the agent needs to know

The agent needs to understand what it's looking at. Without this, it treats enzyme output as generic RAG results.

```python
SYSTEM_PROMPT = """\
You have two tools. Call get_overview ONCE at session start, explore ONCE
when the user asks something. Do not call tools more than twice total.

## Reading enzyme output

Both tools return structured text with entities, catalysts, and documents.

- **Entities** are tags (ingredients, cuisines, techniques) and folders
  (meal categories). Each has frequency, recency, and catalysts.

- **Catalysts** are questions enzyme generated from patterns in the user's
  history. They encode what the user keeps doing across entries without
  explicitly saying it. They explain *why* content surfaced.

- **Results** include the user's own notes — their substitutions, verdicts,
  and opinions. Quote these. Don't paraphrase.

## How to respond

- Lead with specific observations from their patterns, not generic advice
- Notice cross-entry patterns (ingredient swaps, technique habits)
- Suggest things that fit how THIS person operates
- Synthesize across results, don't enumerate them
"""
```

Without the tool-call cap, agents make 5-7 calls with increasingly redundant queries. The cap forces synthesis.

## Data pipeline: what you build

### Entry format

```python
{
    "title": "Roasted Cauliflower with Miso-Tahini Glaze",  # required
    "content": "Whole roasted cauliflower, scored and basted...",
    "notes": "This is the one. The miso-tahini combo does what cheese does but better.",
    "tags": ["vegetarian", "miso", "cauliflower", "cheese"],
    "folder": "saves",
    "created_at": "2026-01-18",
}
```

`title` is required. Everything else is optional but more signal = better catalysts.

### Tags: the entity structure you design

Tags become entities. Entities get catalysts. Catalysts are the retrieval paths. So **tag design is the main integration decision**.

Good tags cluster behavior that recurs across entries:
- Ingredients the user reaches for: `miso`, `tahini`, `cheese`
- Cuisines they gravitate toward: `italian`, `japanese`
- Techniques they use: `roasting`, `braising`
- Behaviors: `make-ahead`, `leftovers`, `weeknight`

Bad tags are either too broad (everything is tagged `food`) or too specific (each entry has a unique tag). Both kill catalyst quality.

**You don't need a perfect taxonomy.** Derive tags from whatever structured data you have (categories, ingredient lists) plus keyword matching on user text. In our test, scanning the user's comment for "cast iron", "dutch oven", "leftover" caught signals that recipe metadata alone missed. See `examples/prepare_nyt_data.py` for a working derivation pipeline.

**Target distribution:**
- 2-3 tags per entry average
- 15+ tags with 10+ entries each (these become catalyst-generating entities)
- <10% of entries with zero tags
- Avoid one catch-all tag/folder that covers >60% of entries

### Folders: optional grouping

Folders act as broad category entities: `soups-and-stews`, `baking-and-desserts`, `pasta-and-noodles`. They're useful when tags are sparse — a recipe with no ingredient tags is still reachable through its folder. But a single `mains` folder with 200/318 entries is too broad to generate useful catalysts. Split or skip.

### Dates

Pass `created_at` as ISO date or epoch millis. Dates feed temporal metadata — recency scores, activity trends in the overview — but catalysts form from content patterns, not timestamps. Historical data works fine.

### Volume

150-300 entries with good tag coverage → 50-60 catalysts across 15-20 entities. Enough for useful search. More entries sharpen catalysts but marginal returns diminish. Below ~50 entries, catalyst quality drops — not enough cross-entry patterns to find.

## What you don't build

- **Query rewriting** — enzyme's catalysts handle personalization at index time
- **Embedding pipeline** — enzyme runs its own embedding model locally (~52MB, no API calls)
- **Preference extraction** — catalysts encode preferences implicitly from content patterns
- **Relevance tuning** — catalyst-based routing outperforms embedding-only search for taste/preference queries
- **User profiling** — the petri overview IS the profile, derived from content not declared

## What can go wrong

- **Sparse tags** → few entities selected → few catalysts → generic retrieval. Fix: enrich tags from content text, not just metadata.
- **Catch-all entities** → catalysts cover everything, distinguish nothing. Fix: split broad categories.
- **Agent over-querying** → 5+ tool calls with redundant queries, wastes tokens. Fix: explicit cap in system prompt.
- **Agent ignoring catalysts** → treats results as plain RAG, misses the "why". Fix: system prompt must explain what catalysts are and that they encode patterns.
- **No user voice in content** → catalysts are generic because the input is generic. Fix: include user annotations, comments, notes — not just titles and metadata.

## Running the example

```bash
cd enzyme-sdk
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]" openai-agents

# Prepare data from NYT Cooking comments dataset
# (requires kaggle dataset: nyt_recipe_comments_Jun25.csv.zip)
python examples/prepare_nyt_data.py

# Run agent test (uses OpenRouter — set OPENROUTER_API_KEY or edit the script)
python examples/agent_test.py
```

## Files

```
enzyme_sdk/
  client.py        — EnzymeClient: ingest, catalyze, petri, render_to_prompt, tool_description
  collection.py    — Collection: high-level wrapper with ingest/search/overview
  document.py      — Document: markdown rendering (filesystem path only)
  store.py         — VaultStore: per-collection directory management
  server.py        — FastAPI multi-tenant REST API

examples/
  agent_test.py       — End-to-end: ingest 318 recipes → init → agent conversation
  prepare_nyt_data.py — NYT CSV → derived tags → enzyme ingest JSON
  nyt_user_data.json  — 318 entries from one NYT commenter (dimmerswitch)
```
