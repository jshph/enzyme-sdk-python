# Enzyme SDK

Python client for [Enzyme](https://enzyme.garden) — a compile step for knowledge bases that gives agents structural understanding of accumulated content.

## Two ingestion paths

### 1. DB ingest (recommended for product integrations)

Pipe structured data directly into the enzyme DB. No markdown files on disk.

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()

# Batch import at signup — user's existing saved recipes
client.ingest(collection="user-123", entries=[
    {
        "title": "Miso Glazed Salmon",
        "content": "Pan-seared salmon with white miso glaze",
        "notes": "Subbed white miso for butter. Way better.",
        "tags": ["seafood", "japanese", "miso"],
        "folder": "saves",
        "created_at": "2025-06-15",
    },
    # ... more entries
])

# Build the index (embeddings + catalysts)
client.init(collection="user-123")

# Search by concept
results = client.catalyze("umami substitutions for dairy", collection="user-123")
print(results.render_to_prompt())
```

Each entry has: `title` (required), `content`, `notes`, `tags`, `links`, `folder`, `created_at`, `metadata`. The SDK handles chunking, hashing, and entity extraction.

`--collection` resolves to `~/.enzyme-sdk/collections/<id>/.enzyme/enzyme.db` — no vault directory needed.

### 2. Filesystem (markdown vaults)

Point at a folder of markdown files. Same as before.

```python
from enzyme_sdk import Collection

coll = Collection.open("~/my-notes", client=client, auto_init=True)
results = coll.search("how they think about creative constraints")
```

## Agent integration

The SDK provides `render_to_prompt()` on response objects and `tool_description()` for harness registration.

```python
from agents import Agent, function_tool
from enzyme_sdk import EnzymeClient

enzyme = EnzymeClient()

@function_tool(description_override=EnzymeClient.tool_description("catalyze"))
def explore(query: str) -> str:
    return enzyme.catalyze(query, collection="user-123").render_to_prompt()

agent = Agent(
    name="Cooking assistant",
    tools=[explore],
    instructions=enzyme.petri(collection="user-123").render_to_prompt(),
)
```

See `examples/agent_test.py` for a full end-to-end example using OpenAI Agent SDK + OpenRouter.

## Data shape: what makes good enzyme input

Enzyme works best when users accumulate choices over time. The data needs two things:

**1. Tags that cluster behavior.** Tags should represent patterns that recur across entries — ingredient preferences, technique habits, cuisine affinities. They become the entities that catalysts are generated for. A tag like "miso" that appears across salmon, banana bread, and eggplant creates a cluster where enzyme can notice the user's fermented-for-dairy substitution pattern.

- Derive tags from both structured metadata (recipe categories, ingredient lists) AND the user's own text (comment mentions of techniques, ingredients, tools)
- Aim for 2-3 tags per entry average, 15+ tags with 10+ entries each
- Tags with <3 entries won't generate meaningful catalysts
- Avoid catch-all tags that apply to everything — they dilute the signal

**2. User voice in the content.** The `notes` field (or inline annotations in `content`) should contain what the user actually thinks — substitutions they made, what worked, what they'd change, their verdicts. Generic descriptions produce generic catalysts. User opinions produce catalysts that encode taste.

### What doesn't matter as much

- **Perfect tag taxonomy.** Derived tags from keyword matching work fine. Enzyme's catalyst generation finds the cross-cutting patterns that individual tags miss.
- **Huge volume.** 150-300 entries with good tag coverage produce 50-60 catalysts across 15-20 entities — enough for useful search. More entries sharpen the catalysts but the marginal return diminishes.
- **Recent data.** Historical data works. Dates flow through for temporal metadata (recency scores, activity trends) but catalysts form from content patterns, not timestamps.

### Entity structure

Enzyme selects ~20 top entities (by frequency × recency) and generates catalysts for each. Entities are:

- **Tags** — ingredient, cuisine, technique clusters (e.g., "miso", "italian", "braising")
- **Folders** — behavioral/category clusters (e.g., "saves", "soups-and-stews", "baking-and-desserts")
- **Links** — wikilink references (e.g., people, sources)

Each entity gets 3+ catalysts — AI-generated questions that probe what's latent in that cluster. The catalysts are the retrieval paths: queries match against them, not against document text directly.

Tested with 318 NYT Cooking recipe comments from one prolific commenter, with tags derived from recipe names + comment text. See `examples/prepare_nyt_data.py` for the full preprocessing pipeline.

## System prompt guidance for agents

When an agent uses enzyme tools, it receives JSON output. The system prompt should explain:

1. **petri output** returns entities with their catalysts, frequency, and activity trends. Use it once at session start to understand the user's landscape.
2. **catalyze output** returns matched documents (with the user's notes) and the catalysts that routed the match. The catalysts explain *why* content surfaced.
3. **Call tools sparingly** — one overview + one search is usually enough. The agent should synthesize, not enumerate.
4. **Lead with the user's words** — quote from their notes/comments, don't paraphrase generically.

See the `SYSTEM_PROMPT` in `examples/agent_test.py` for a working example.

## Files

```
enzyme_sdk/
  client.py        — EnzymeClient (binary wrapper, ingest, render_to_prompt)
  collection.py    — Collection (add, ingest, search, overview)
  document.py      — Document (content → markdown for filesystem path)
  store.py         — VaultStore (per-collection directories)
  server.py        — FastAPI multi-tenant API

examples/
  agent_test.py       — End-to-end: ingest → init → agent conversation
  prepare_nyt_data.py — Preprocessing pipeline for NYT Cooking comments
  nyt_user_data.json  — 318 entries from one NYT commenter (dimmerswitch)
```
