"""End-to-end test: load CSV → ingest → build index → agent conversation.

Uses OpenAI Agent SDK + Gemini 3 Flash Preview via OpenRouter.
Data: NYT Cooking recipe comments from one prolific commenter.

Run:
    python examples/prepare_nyt_data.py   # generates nyt_user_data.json
    python examples/agent_test.py         # runs this
"""

import asyncio
import csv
import io
import json
import os
import re
import shutil
import sys
import zipfile
from datetime import datetime, timezone

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
USER = os.environ.get("ENZYME_TEST_USER", "es")
COLLECTION_ID = f"nyt-{USER}"
OPENROUTER_KEY = os.environ.get(
    "OPENROUTER_API_KEY",
    "",
)
MODEL = "google/gemini-3-flash-preview"

# Pre-generated data (from prepare_nyt_data.py <user>)
DATA_PATH = os.path.join(os.path.dirname(__file__), f"nyt_{USER}_data.json")


# ── Loading data ─────────────────────────────────────────────────────────────
#
# This is the part you'd adapt for your own dataset. The helper function
# maps your rows to enzyme entries — title, notes (user voice), labels
# (how enzyme clusters), folder (broad category), and date.
#
# The NYT dataset has no tags. We derive them from recipe names and comment
# text — scanning for ingredients, cuisines, and techniques the user mentions.

LABEL_KEYWORDS = {
    # proteins
    "chicken": "chicken", "turkey": "turkey", "pork": "pork", "beef": "beef",
    "lamb": "lamb", "salmon": "salmon", "shrimp": "shrimp", "fish": "fish",
    "sausage": "sausage", "egg": "eggs", "tofu": "tofu",
    "lentil": "lentils", "chickpea": "chickpeas", "bean": "beans",
    # cuisines
    "pasta": "italian", "risotto": "italian", "lasagna": "italian",
    "gnocchi": "italian", "bolognese": "italian", "ziti": "italian",
    "miso": "japanese", "soba": "japanese", "ramen": "japanese",
    "korean": "korean", "kimchi": "korean",
    "curry": "southeast-asian", "thai": "thai",
    "taco": "mexican", "enchilada": "mexican", "tortilla": "mexican",
    "dal": "indian", "masala": "indian", "shawarma": "middle-eastern",
    "shakshuka": "mediterranean",
    # techniques & categories
    "roast": "roasting", "braise": "braising", "grill": "grilling",
    "stew": "stew", "soup": "soup", "salad": "salad",
    "cake": "baking", "cookie": "baking", "bread": "baking", "pie": "baking",
    # from comment text — user's own words reveal patterns
    "cast iron": "cast-iron", "dutch oven": "dutch-oven",
    "leftover": "leftovers", "make ahead": "make-ahead",
    "parmesan": "cheese", "feta": "cheese", "ricotta": "cheese",
    "lemon": "citrus", "lime": "citrus",
    "chili": "heat", "chipotle": "heat", "cayenne": "heat",
}

FOLDER_RULES = [
    (["baking"], "baking-and-desserts"),
    (["pasta", "italian"], "pasta-and-noodles"),
    (["soup", "stew"], "soups-and-stews"),
    (["salad"], "salads"),
]


def row_to_entry(recipe_name: str, comment: str, timestamp: float) -> dict:
    """Map a CSV row to an enzyme ingest entry.

    This is the adapter function — change this for your dataset.
    """
    # Derive labels from recipe name + comment text
    text = (recipe_name + " " + comment).lower()
    labels = sorted({tag for kw, tag in LABEL_KEYWORDS.items() if kw in text})

    # Derive folder from labels
    folder = "mains"
    for trigger_tags, folder_name in FOLDER_RULES:
        if any(t in labels for t in trigger_tags):
            folder = folder_name
            break

    # Clean HTML from comment
    clean = re.sub(r"<br\s*/?>", "\n", comment)
    clean = re.sub(r"<[^>]+>", "", clean)
    clean = clean.replace("&amp;", "&").replace("&quot;", '"').strip()

    return {
        "title": recipe_name.replace("-", " ").title(),
        "content": f"NYT Cooking recipe: {recipe_name.replace('-', ' ').title()}",
        "notes": clean,
        "tags": labels,
        "folder": folder,
        "created_at": datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d"),
    }


def load_entries() -> list[dict]:
    """Load pre-generated entries, or build from CSV if available."""
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH) as f:
            return json.load(f)["entries"]

    raise FileNotFoundError(
        f"{DATA_PATH} not found. Run: python examples/prepare_nyt_data.py"
    )


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a cooking assistant that knows this user's actual cooking history — \
recipes they've cooked and commented on from NYT Cooking over several years.

You have two tools. Use them sparingly:

1. **get_overview** — call ONCE at the start to see the user's clusters \
(ingredient types, cuisine affinities, techniques) and their catalysts \
(thematic questions enzyme generated about patterns in their cooking). \
This tells you what they cook, how often, and what's been active.

2. **explore** — search their history by concept. Call ONCE with a query \
shaped by what you learned from the overview. Results include the user's \
own words about what they changed, what worked, what they'd do differently, \
and the catalysts that routed the match.

## How to respond

- Lead with specific observations from their cooking patterns, not generic advice
- Quote their own words from comments when relevant
- Notice cross-recipe patterns (ingredient substitutions, technique preferences)
- Suggest things that fit how THIS person cooks
- Synthesize across results — don't enumerate them
- **Do not call tools more than twice total** (1 overview + 1 search)
"""


# ── Tools & Agent ─────────────────────────────────────────────────────────────

enzyme = EnzymeClient(enzyme_bin=ENZYME_BIN)


def make_tools(collection_id: str):
    @function_tool(
        name_override="explore",
        description_override=EnzymeClient.tool_description("catalyze"),
    )
    def explore(query: str) -> str:
        """Search the user's cooking history by concept."""
        return enzyme.catalyze(query, collection=collection_id).render_to_prompt()

    @function_tool(
        name_override="get_overview",
        description_override=EnzymeClient.tool_description("petri"),
    )
    def get_overview() -> str:
        """Get an overview of the user's cooking patterns."""
        return enzyme.petri(collection=collection_id, top=15).render_to_prompt()

    return [explore, get_overview]


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
    shutil.rmtree(collection_path, ignore_errors=True)

    # ── Step 1: Load and ingest ──────────────────────────────────────────
    entries = load_entries()
    print(f"Loaded {len(entries)} entries ({entries[0]['created_at']} to {entries[-1]['created_at']})")

    result = enzyme.ingest(collection=COLLECTION_ID, entries=entries)
    print(f"Ingested {result['documents_ingested']} documents in {result['duration_ms']}ms")

    # ── Step 2: Build index ──────────────────────────────────────────────
    print("Building index...")
    init_result = enzyme.init(collection=COLLECTION_ID)
    if init_result:
        print(f"  Catalysts: {init_result.get('catalysts_generated', 0)}")
        print(f"  Duration: {init_result.get('duration_ms', 0)}ms")

    # ── Step 3: Agent conversation ───────────────────────────────────────
    user_msg = (
        "I'm hosting a dinner party this weekend for 6 people. "
        "A couple of them are vegetarian. What should I make?"
    )
    print(f"\nUser: {user_msg}\n")

    result = await Runner.run(agent, input=user_msg)
    print(f"Agent:\n{result.final_output}\n")

    tool_calls = [
        item for item in result.new_items
        if hasattr(item, "raw_item")
        and hasattr(item.raw_item, "type")
        and item.raw_item.type == "function_call"
    ]
    if tool_calls:
        print(f"[{len(tool_calls)} tool call(s): {', '.join(tc.raw_item.name for tc in tool_calls)}]")

    # Cleanup
    shutil.rmtree(collection_path, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
