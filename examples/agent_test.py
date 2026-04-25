"""End-to-end example: prepared entries -> auto tags -> ingest -> agent.

Run:
    python examples/prepare_nyt_data.py es
    python examples/agent_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from agents import Agent, ModelSettings, Runner, function_tool
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from enzyme_sdk import EnzymeClient


ENZYME_BIN = os.environ.get("ENZYME_BIN", "enzyme")
USER = os.environ.get("ENZYME_TEST_USER", "es")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
DATA_PATH = Path(
    os.environ.get(
        "ENZYME_NYT_DATA",
        Path(tempfile.gettempdir()) / f"nyt_{USER}_data.json",
    )
)
GRANULARITY = os.environ.get("ENZYME_CLUSTER_GRANULARITY", "balanced")


def entry_text(entry: dict) -> str:
    return f"{entry['title']}\n\n{entry['notes']}"


def load_entries() -> tuple[list[dict], list[dict]]:
    if not DATA_PATH.exists():
        raise FileNotFoundError(
            f"{DATA_PATH} not found. Run: python examples/prepare_nyt_data.py {USER}"
        )
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return data["cluster_entries"], data["entries"]


SYSTEM_PROMPT = """\
You are a cooking assistant that knows this user's actual cooking history.

Use get_overview once to understand the user's clusters and catalysts. Use
explore once when a specific recommendation needs supporting recipe notes.
Quote the user's own words. Synthesize across results instead of listing them.
Do not call tools more than twice total.
"""


enzyme = EnzymeClient(enzyme_bin=ENZYME_BIN)


def make_tools(vault: Path):
    @function_tool(
        name_override="explore",
        description_override=EnzymeClient.tool_description("catalyze"),
    )
    def explore(query: str) -> str:
        return enzyme.catalyze(query, vault=vault).render_to_prompt()

    @function_tool(
        name_override="get_overview",
        description_override=EnzymeClient.tool_description("petri"),
    )
    def get_overview() -> str:
        return enzyme.petri(vault=vault, top=15).render_to_prompt()

    return [explore, get_overview]


def make_agent(vault: Path) -> Agent:
    client_kwargs = {"api_key": OPENAI_API_KEY}
    if OPENAI_BASE_URL:
        client_kwargs["base_url"] = OPENAI_BASE_URL
    openai_client = AsyncOpenAI(**client_kwargs)
    model = OpenAIChatCompletionsModel(
        model=MODEL,
        openai_client=openai_client,
    )
    return Agent(
        name="Cooking assistant",
        model=model,
        instructions=SYSTEM_PROMPT,
        tools=make_tools(vault),
        model_settings=ModelSettings(tool_choice="auto", temperature=0.7),
    )


async def main():
    run_root = Path(tempfile.mkdtemp(prefix="enzyme-sdk-nyt-"))
    vault = run_root / f"nyt-{USER}"
    home = run_root / "home"
    os.environ["HOME"] = str(home)

    try:
        cluster_entries, entries = load_entries()
        print(f"Loaded {len(cluster_entries)} cluster entries from {DATA_PATH}")
        print(f"Selected {len(entries)} entries for {USER!r}")

        cluster_index = enzyme.build_entry_cluster_index(
            cluster_entries,
            text=entry_text,
            granularity=GRANULARITY,
        )
        assigned = cluster_index.assign(
            entries,
            text=entry_text,
            target_field="auto_tags",
        )
        ingest_entries = [
            {
                "title": entry["title"],
                "content": entry["content"],
                "notes": entry["notes"],
                "tags": entry.get("auto_tags", []),
                "created_at": entry["created_at"],
                "metadata": entry["metadata"],
            }
            for entry in assigned.entries
        ]

        print(f"Built {len(cluster_index.clusters)} automatic clusters")
        print(f"Assigned auto tags to {len({a.entry_index for a in assigned.assignments})} entries")

        result = enzyme.ingest(vault=vault, entries=ingest_entries)
        print(f"Ingested {result['documents_ingested']} documents in {result['duration_ms']}ms")

        print("Building index...")
        init_result = enzyme.init(vault=vault)
        if init_result:
            print(f"  Catalysts: {init_result.get('catalysts_generated', 0)}")
            print(f"  Duration: {init_result.get('duration_ms', 0)}ms")

        user_msg = (
            "I'm hosting a dinner party this weekend for 6 people. "
            "A couple of them are vegetarian. What should I make?"
        )
        print(f"\nUser: {user_msg}\n")

        result = await Runner.run(make_agent(vault), input=user_msg)
        print(f"Agent:\n{result.final_output}\n")
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
