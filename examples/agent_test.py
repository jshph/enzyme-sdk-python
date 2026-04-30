"""End-to-end example: decorated connector -> Enzyme index -> agent.

Run:
    python examples/agent_test.py
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(Path(__file__).resolve().parents[1] / ".env")

from enzyme_sdk import EnzymeClient
from enzyme_sdk.client import EnzymeError
from enzyme_sdk.store import VaultStore


ENZYME_BIN = os.environ.get("ENZYME_BIN", "enzyme")
USER = os.environ.get("ENZYME_TEST_USER", "es")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
HOST_ENZYME_HOME = Path(os.environ.get("ENZYME_HOME", Path.home() / ".enzyme"))


SYSTEM_PROMPT = """\
You are a cooking assistant that knows this user's actual cooking history.

Call get_overview first to understand the user's catalysts. Then call explore
with a query informed by that overview before recommending dishes.

Do not recommend any dish unless it is supported by matched recipe notes from
explore. Quote at least two specific recipe names or user notes from explore.
Synthesize across results instead of listing them. Use exactly these two tool
calls unless a tool fails.
"""


def make_tools(connector, user_id: str):
    try:
        from agents import function_tool
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing example dependency 'openai-agents'. "
            "Install with: pip install -e '.[dev,cluster,examples]'"
        ) from exc

    @function_tool(
        name_override="explore",
        description_override=EnzymeClient.tool_description("catalyze"),
    )
    def explore(query: str) -> str:
        return connector.search(user_id, query).render_to_prompt()

    @function_tool(
        name_override="get_overview",
        description_override=EnzymeClient.tool_description("petri"),
    )
    def get_overview() -> str:
        return connector.overview(user_id, top=15).render_to_prompt()

    return [explore, get_overview]


def make_agent(connector, user_id: str):
    try:
        from agents import Agent, ModelSettings
        from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
        from openai import AsyncOpenAI
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Missing example dependency 'openai-agents'. "
            "Install with: pip install -e '.[dev,cluster,examples]'"
        ) from exc

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
        tools=make_tools(connector, user_id),
        model_settings=ModelSettings(tool_choice="auto", temperature=0.7),
    )


async def main():
    run_root = Path(tempfile.mkdtemp(prefix="enzyme-sdk-nyt-"))
    os.environ["HOME"] = str(run_root / "home")
    os.environ["ENZYME_HOME"] = str(run_root / "enzyme-home")
    enzyme_home = Path(os.environ["ENZYME_HOME"])
    enzyme_home.mkdir(parents=True, exist_ok=True)
    auth_src = HOST_ENZYME_HOME / "auth.json"
    if auth_src.exists():
        shutil.copy2(auth_src, enzyme_home / "auth.json")

    try:
        from examples.run_mcp_server import client, hydrate_recipes

        client._enzyme_client = EnzymeClient(enzyme_bin=ENZYME_BIN)
        client._store = VaultStore(run_root / "collections")
        client._collections_base = Path(client._store.base_path)

        activities = list(hydrate_recipes(USER))
        counts = Counter(type(activity).__name__ for activity in activities)
        print(f"Hydrated {sum(counts.values())} activities for {USER!r}:", flush=True)
        for name, count in sorted(counts.items()):
            print(f"  {name}: {count}", flush=True)

        entries = [client._entry_from_item(activity) for activity in activities]
        collections = Counter(
            collection
            for entry in entries
            for collection in entry.get("collections", [])
        )
        print("Activity collections:", flush=True)
        for name, count in sorted(collections.items()):
            print(f"  {name}: {count}", flush=True)

        print("Building index via EnzymeConnector decorators...", flush=True)
        try:
            status = client.connect_user(USER)
        except EnzymeError as exc:
            raise SystemExit(
                f"Could not build the local Enzyme index: {exc}\n"
                "Run `enzyme login`, then rerun this example."
            ) from exc
        print(f"Indexed {status['entries']} activities")

        overview = client.overview(USER, top=15)
        print(
            f"Catalyzed overview: {overview.total_entities} entities, "
            f"{sum(len(e.catalysts) for e in overview.entities)} catalysts shown"
        )
        for entity in overview.entities[:5]:
            print(
                f"  {entity.entity_type}: {entity.name} "
                f"({len(entity.catalysts)} catalysts)"
            )

        observation_entities = [
            entity for entity in overview.entities
            if "observed" in entity.name or "preference" in entity.name
        ]
        observation_catalysts = sum(len(entity.catalysts) for entity in observation_entities)
        print(
            "Observed-preference catalyst coverage: "
            f"{len(observation_entities)} entities, {observation_catalysts} catalysts shown"
        )

        if not OPENAI_API_KEY:
            raise SystemExit("OPENAI_API_KEY is required for the agent response step.")

        try:
            from agents import Runner
        except ModuleNotFoundError as exc:
            raise SystemExit(
                "Missing example dependency 'openai-agents'. "
                "Install with: pip install -e '.[dev,cluster,examples]'"
            ) from exc

        user_msg = (
            "I'm hosting a dinner party this weekend for 6 people. "
            "A couple of them are vegetarian. What should I make?"
        )
        grounding_query = (
            "vegetarian dinner party recipes repeat-worthy seasonal vegetables "
            "make ahead substitutions served for dinner"
        )
        grounding = client.search(USER, grounding_query, limit=8)
        print(f"\nUser: {user_msg}\n")

        grounded_input = (
            f"{user_msg}\n\n"
            "Use this Enzyme overview and search evidence before answering.\n\n"
            f"{overview.render_to_prompt()}\n\n"
            f"{grounding.render_to_prompt()}"
        )

        result = await Runner.run(make_agent(client, USER), input=grounded_input)
        print(f"Agent:\n{result.final_output}\n")
    finally:
        shutil.rmtree(run_root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
