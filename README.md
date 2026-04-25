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

Enzyme generates **catalysts** — thematic questions — from each tag cluster in the user's data. When the agent queries, it matches against those questions, not document text.

```
Agent query:     "vegetarian dinner ideas"

Catalyst match:  "What does the commitment to vegetarianism cost when
                  Italian sausage and broccoli rabe are on the menu?"
```

The agent's query is generic. The catalyst is specific to this user — someone who saves sausage dishes despite cooking vegetarian most of the time. The personalization happened at index time.

## Get started

```python
from enzyme_sdk import EnzymeClient

client = EnzymeClient.ensure_installed()  # ~11MB binary + ~52MB embedding model

# Ingest user data — no files on disk, straight to the DB
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

# Build index — generates embeddings (local, no API) + catalysts (needs LLM API key).
# ~6s for 318 entries. Set OPENAI_API_KEY or configure via ~/.enzyme/config.toml.
client.init(collection="user-123")

# Search — returns matched docs + the catalysts that routed them
results = client.catalyze("comfort food", collection="user-123")
print(results.render_to_prompt())  # formatted text an LLM can use directly
```

Each collection is a per-user index at `~/.enzyme-sdk/collections/<id>/`. Ingest is additive — call it on each new user action, then `init` or `refresh` to rebuild catalysts.

## The one design decision: tags

Tags become the entities that catalysts are generated for. They're the retrieval paths.

A tag like `miso` that appears on salmon, banana bread, and eggplant creates a cluster. Enzyme sees the user keeps substituting miso for dairy fats and generates a catalyst about that pattern. Without the tag, those three recipes are unrelated documents.

Derive tags from whatever you have — recipe categories, ingredient lists, user text. In our test, scanning the user's comments for words like "cast iron" and "leftover" caught patterns that metadata alone missed.

Aim for 2-3 tags per entry and 15+ tags with 10+ entries each. Below that, catalysts don't have enough cross-entry patterns to work with.

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

The system prompt needs to tell the agent: (1) call tools at most twice, (2) catalysts explain *why* results surfaced — use them, (3) quote the user's own words from the results. Without the tool-call cap, agents make 5-7 redundant calls. See `examples/agent_test.py` for the full system prompt.

## Example

```bash
pip install -e ".[dev]" openai-agents
python examples/prepare_nyt_data.py   # prepare 318 entries from NYT Cooking dataset
python examples/agent_test.py         # run agent via OpenRouter (Gemini 3 Flash)
```

The example ingests one prolific NYT Cooking commenter's history (318 recipes, 2017-2021), builds an index (60 catalysts across 20 entities in ~6s), and runs an agent conversation that produces the dinner party response shown above.
