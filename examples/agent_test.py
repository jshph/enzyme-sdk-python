"""End-to-end test: ingest real NYT Cooking data → run agent with enzyme tools.

Uses OpenAI Agent SDK + Gemini 3 Flash Preview via OpenRouter.
Data: 318 recipe comments from one prolific NYT Cooking commenter (dimmerswitch).
"""

import asyncio
import json
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agents import Agent, Runner, function_tool, ModelSettings
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from enzyme_sdk import EnzymeClient

# ── Config ────────────────────────────────────────────────────────────────────

ENZYME_BIN = os.environ.get(
    "ENZYME_BIN",
    os.path.join(
        os.path.dirname(__file__),
        "../../enzyme-rust/.claude/worktrees/sdk-db-ingest/target/debug/enzyme",
    ),
)
COLLECTION_ID = "nyt-dimmerswitch"
OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    "",
)
MODEL = "google/gemini-3-flash-preview"
DATA_PATH = os.path.join(os.path.dirname(__file__), "nyt_user_data.json")

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a cooking assistant that knows this user's actual cooking history — \
318 recipes they've cooked and commented on from NYT Cooking over 4 years.

You have two tools. Use them sparingly:

1. **get_overview** — call ONCE at the start to see the user's active entities \
(ingredient tags, cuisine types, technique clusters) and their catalysts \
(thematic questions enzyme generated about patterns in their cooking). \
This tells you what they cook, how often, and what's trending.

2. **explore** — search their history by concept. Call ONCE with a query shaped \
by what you learned from the overview. The query should use natural language \
about cooking patterns, not keywords. Results include matched recipe comments \
(the user's own words about what they changed, what worked, what they'd do \
differently) and the catalysts that routed the match.

## Reading enzyme output

Both tools return structured text. Key elements:

- **Entities** are tags (ingredients like "miso", cuisines like "italian", \
techniques like "braising") and folders (meal categories like "soups-and-stews"). \
Each has frequency, recency, and catalysts.

- **Catalysts** are questions enzyme generated from patterns in the user's \
comments — they encode what the user keeps doing across recipes without \
explicitly saying it. A catalyst like "What does the tahini finish replace \
across these dishes?" connects comments on 5 different recipes.

- **Search results** have file_path (the recipe), content, similarity score, \
and the user's notes (their actual comment — substitutions, verdicts, tips).

## How to respond

- Lead with specific observations from their cooking patterns, not generic advice
- Quote their own words from comments when relevant
- Notice cross-recipe patterns (ingredient substitutions, technique preferences)
- Suggest things that fit how THIS person cooks, not generic recommendations
- Keep responses focused — don't list every result, synthesize
- **Do not call tools more than twice total** (1 overview + 1 search)
"""


# ── Tools ─────────────────────────────────────────────────────────────────────

enzyme = EnzymeClient(enzyme_bin=ENZYME_BIN)


def make_tools(collection_id: str):
    @function_tool(
        name_override="explore",
        description_override=EnzymeClient.tool_description("catalyze"),
    )
    def explore(query: str) -> str:
        """Search the user's cooking history by concept."""
        result = enzyme.catalyze(query, collection=collection_id)
        return result.render_to_prompt()

    @function_tool(
        name_override="get_overview",
        description_override=EnzymeClient.tool_description("petri"),
    )
    def get_overview() -> str:
        """Get an overview of the user's cooking patterns."""
        result = enzyme.petri(collection=collection_id, top=15)
        return result.render_to_prompt()

    return [explore, get_overview]


# ── Agent ─────────────────────────────────────────────────────────────────────

openrouter_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_KEY,
)

model = OpenAIChatCompletionsModel(
    model=MODEL,
    openai_client=openrouter_client,
)

agent = Agent(
    name="Cooking assistant",
    model=model,
    instructions=SYSTEM_PROMPT,
    tools=make_tools(COLLECTION_ID),
    model_settings=ModelSettings(tool_choice="auto", temperature=0.7),
)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    collection_path = os.path.expanduser(
        f"~/.enzyme-sdk/collections/{COLLECTION_ID}"
    )

    # Clean previous run
    shutil.rmtree(collection_path, ignore_errors=True)

    # Step 1: Ingest
    print("Loading data...")
    with open(DATA_PATH) as f:
        data = json.load(f)
    entries = data["entries"]
    print(f"  {len(entries)} recipe entries, dates {entries[0]['created_at']} to {entries[-1]['created_at']}")

    print("Ingesting into enzyme...")
    result = enzyme.ingest(collection=COLLECTION_ID, entries=entries)
    print(f"  {result['documents_ingested']} documents ingested in {result['duration_ms']}ms")

    # Step 2: Init (embeddings + catalysts)
    print("Building index (embeddings + catalysts)...")
    init_result = enzyme.init(collection=COLLECTION_ID)
    if init_result:
        caps = init_result.get("capabilities", {})
        print(f"  Semantic search: {caps.get('semantic_search', False)}")
        print(f"  Catalysts generated: {init_result.get('catalysts_generated', 0)}")
        print(f"  Duration: {init_result.get('duration_ms', 0)}ms")

    # Step 3: Agent conversation
    print("\n" + "=" * 70)

    prompts = [
        "I'm hosting a dinner party this weekend for 6 people. A couple of them are vegetarian. What should I make?",
    ]

    for user_msg in prompts:
        print(f"\nUser: {user_msg}\n")

        try:
            result = await Runner.run(agent, input=user_msg)
            print(f"Agent:\n{result.final_output}\n")

            # Show tool usage summary
            tool_calls = [
                item for item in result.new_items
                if hasattr(item, "raw_item")
                and hasattr(item.raw_item, "type")
                and item.raw_item.type == "function_call"
            ]
            if tool_calls:
                print(f"  [{len(tool_calls)} tool call(s): {', '.join(tc.raw_item.name for tc in tool_calls)}]")
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "=" * 70)

    # Cleanup
    shutil.rmtree(collection_path, ignore_errors=True)
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
