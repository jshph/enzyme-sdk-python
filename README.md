# Enzyme SDK

Python client for [Enzyme](https://enzyme.garden) — a compile step for knowledge bases that gives agents structural understanding of accumulated content.

## Quickstart

```bash
pip install enzyme-sdk
```

```python
from enzyme_sdk import EnzymeClient, Collection

# Downloads the binary + embedding model if not already installed.
client = EnzymeClient.ensure_installed()

# Point at a folder of markdown files.
coll = Collection.open("~/my-notes", client=client, auto_init=True)

# Search by concept — not keyword.
results = coll.search("how they think about creative constraints")
for r in results.results:
    print(f"[{r.similarity:.3f}] {r.file_path}")
```

That's it. `ensure_installed()` handles the binary. `auto_init=True` runs the indexing pipeline on first use. After that, searches are local and fast.

## What happens under the hood

1. **Install** — downloads a platform binary (~11MB) and an embedding model (~52MB) to `~/.local/bin/`. No external services needed for search.
2. **Init** — reads the markdown files, extracts entities (tags, links, folders), and generates thematic questions that characterize what each entity's documents are about. This is the compile step.
3. **Search** — your query matches against the precomputed questions, which route to the right documents. A broad query works because the questions already encode the specific patterns in the content.
4. **Refresh** — after adding new content, call `coll.refresh()` to update the index. Only re-processes changed files.

## Adding content

```python
from enzyme_sdk import Document

coll.add(Document.from_text(
    "Session — typography review",
    "Sarah advanced the serif option for headings. 'The serif carries "
    "intention. The sans carries information.' Rejected monospace.",
    tags=["typography", "design-session"],
    links=["Sarah", "Meridian"],
), folder="Sessions")

coll.refresh()
```

The SDK writes markdown with tags and wikilinks. Enzyme indexes it. You don't manage the format.

## Using search results in an agent

```python
# Agent needs preference context before generating design concepts.
results = coll.search("Sarah typography preferences for functional layouts")

# Each result is a full document — not a fragment.
top = results.results[0]
print(top.content)  # Full session prose: decisions, quotes, rationale.

# Contributing catalysts explain *why* this was retrieved:
for c in results.top_contributing_catalysts:
    print(f"[{c.entity}] {c.text}")
    # e.g. "How does Sarah's serif/sans split adapt across editorial
    #        vs. functional contexts?"

# Inject results + catalyst signals into the generation prompt.
# The catalyst tells the agent what pattern to focus on.
```

## See what the index understands

```python
overview = coll.overview(top=5)
for entity in overview.entities:
    print(f"{entity.name} ({entity.entity_type})")
    for q in entity.catalysts:
        print(f"  ? {q['text']}")
```

This shows the conceptual structure Enzyme built — the entities it tracks and the questions it generated for each. These questions are the retrieval paths. They evolve as content accumulates.

## API Server

For multi-tenant use:

```bash
export ENZYME_SDK_API_KEY="your-key"
python -m enzyme_sdk  # starts on port 8420
```

Endpoints: create/delete collections, ingest documents, search, refresh.

## Files

```
enzyme_sdk/
  client.py        — EnzymeClient (binary wrapper + auto-install)
  collection.py    — Collection (add, search, overview, refresh)
  document.py      — Document (content → markdown)
  store.py         — VaultStore (per-collection directories)
  server.py        — FastAPI multi-tenant API
```
