# Enzyme SDK

Enzyme pre-indexes user content so agents can access patterns the user never articulated. You send structured data in, enzyme generates thematic questions from it at build time, and agents search against those questions instead of raw embeddings.

## What the agent produces

A cooking app user has 318 saved recipes with annotations. The agent gets asked: *"I'm hosting a dinner party, a couple friends are vegetarian. What should I make?"*

> You've made **Baked Polenta with Ricotta and Parmesan** before for 5 people and called it a "Keeper!" ... You noted that the roasting time for the mushrooms is "key for texture."
>
> Your **Roasted Tomato and White Bean Stew** — you wrote that "cooking beans from scratch... makes such a flavor difference." You recommended tucking parmesan rinds into the bean pot.
>
> For the meat-eaters, your **Lamb Meatballs with Spiced Tomato Sauce** could work alongside — you called it your "fav in 2017." You once wrote that the sauce "would make shoe leather taste good."

This isn't template filling. The agent found these recipes because enzyme's index had already noticed this user reaches for parmesan rinds, prefers slow-cooked beans, and has a specific set of "keeper" recipes. A vector search for "vegetarian dinner" wouldn't surface the lamb meatball connection or know that the polenta was a crowd-tested keeper.

## How it works

Enzyme groups your data into clusters and generates **catalysts** — thematic questions — for each cluster. When the agent queries, it matches against those questions, not document text.

```
Agent query:     "vegetarian dinner ideas"

Catalyst match:  "What does the commitment to vegetarianism cost when
                  Italian sausage and broccoli rabe are on the menu?"
```

The agent's query is generic. The catalyst is specific to this user — someone who saves sausage dishes despite cooking vegetarian most of the time. The personalization happened at index time.

### How clusters form

Enzyme clusters content using labels you attach to entries. In the cooking example, entries get labels like `miso`, `italian`, `braising` — derived from recipe categories and the user's own comment text. Miso salmon, miso banana bread, and miso eggplant land in the same cluster. Enzyme sees the user keeps substituting miso for dairy fats and generates a question about that pattern.

The labels don't need to be perfect or hand-curated. Derive them from whatever you have: product categories, ingredient lists, keywords in user text. In our test, scanning comments for "cast iron" and "leftover" caught patterns that recipe metadata alone missed. See `examples/prepare_nyt_data.py` for a working derivation pipeline.

Without any labels, enzyme still clusters by folder and content similarity — but labeling lets you tell it which groupings matter for your domain.

## Get started

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()  # ~11MB binary + ~52MB embedding model

# Ingest user data — no files on disk, straight to the DB
client.ingest(collection="user-123", entries=[
    {
        "title": "Miso Glazed Salmon",
        "notes": "Subbed white miso for butter. Way better.",
        "tags": ["seafood", "japanese", "miso"],  # labels — how enzyme clusters
        "folder": "saves",
        "created_at": "2025-06-15",
    },
    # ...
])

# Build index — embeddings run locally, catalysts need an LLM API key.
# ~6s for 318 entries. Set OPENAI_API_KEY or configure via ~/.enzyme/config.toml.
client.init(collection="user-123")

# Search — returns matched docs + the catalysts that routed them
results = client.catalyze("comfort food", collection="user-123")
print(results.render_to_prompt())  # formatted text an LLM can use directly
```

Each collection is a per-user index at `~/.enzyme-sdk/collections/<id>/`. Ingest is additive — call it on each new user action, then `init` or `refresh` to rebuild.

## Agent integration

Two tools: `petri` (overview at session start) and `catalyze` (search mid-conversation).

```python
from agents import Agent, function_tool

enzyme = EnzymeClient()

@function_tool(description_override=EnzymeClient.tool_description("catalyze"))
def explore(query: str) -> str:
    return enzyme.catalyze(query, collection=user_id).render_to_prompt()

@function_tool(description_override=EnzymeClient.tool_description("petri"))
def get_overview() -> str:
    return enzyme.petri(collection=user_id).render_to_prompt()
```

The system prompt needs three things: (1) cap tool calls to two — without this, agents make 5-7 redundant calls, (2) explain that catalysts tell the agent *why* results surfaced, (3) tell it to quote the user's own words from results. See `examples/agent_test.py` for the full working prompt.

## Example

```bash
pip install -e ".[dev]" openai-agents
python examples/prepare_nyt_data.py   # prepare 318 entries from NYT Cooking dataset
python examples/agent_test.py         # run agent via OpenRouter (Gemini 3 Flash)
```

Ingests one prolific NYT Cooking commenter's history (318 recipes, 2017-2021), builds an index (60 catalysts in ~6s), and runs the agent conversation that produces the dinner party response above.
