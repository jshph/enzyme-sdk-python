"""Run a local MCP server backed by Enzyme with real NYT recipe data.

Usage:
    python examples/run_mcp_server.py                    # local only
    python examples/run_mcp_server.py --ngrok             # expose via ngrok
    python examples/run_mcp_server.py --ngrok --port 8080 # custom port
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
import sys
from typing import Iterable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from enzyme_sdk.enzyme import EnzymeConnector, enzyme
from enzyme_sdk.activity import Activity
from examples.prepare_nyt_data import DEFAULT_INPUT, USERS, load_rows, rows_to_entries


@dataclass
class RecipeComment:
    id: str
    user_id: str
    recipe_name: str
    comment: str
    created_at: str
    tags: list[str]


@dataclass
class ObservedPreference:
    id: str
    user_id: str
    topic: str
    summary: str
    evidence: str
    created_at: str


# Load real NYT recipe data
rows = load_rows(DEFAULT_INPUT)
all_entries = rows_to_entries(rows, include_user_keys=set(USERS))

entries_by_user: dict[str, list[dict]] = {}
for entry in all_entries:
    uid = entry["metadata"]["user_key"]
    entries_by_user.setdefault(uid, []).append(entry)

client = EnzymeConnector(
    display_name="NYT Cooking",
    description="Real NYT recipe comments and cooking notes",
    content_label="cooking notes",
    catalyze_tool="catalyze_cooking_notes",
    catalyze_description=(
        "Search this user's cooking history — recipe annotations, substitutions, "
        "results, and personal notes built over years of cooking. Broad queries "
        "work well. Results include the thematic signals that connected the query "
        "to the content."
    ),
    profile_tool="get_cooking_profile",
    profile_description=(
        "See what this user's cooking history reveals — recurring ingredients, "
        "techniques they've adopted or abandoned, and the thematic questions that "
        "characterize each area. Call this first to understand what you're working with."
    ),
    system_prompt=(
        "You are a cooking assistant that knows this user's actual cooking history. "
        "Use get_cooking_profile once to understand their patterns. Use catalyze_cooking_notes "
        "when a specific recommendation needs supporting notes. Quote the user's own "
        "words. Synthesize across results instead of listing them."
    ),
)


@enzyme.hydrate(client)
def hydrate_recipes(user_id: str) -> Iterable[RecipeComment | ObservedPreference]:
    activities: list[RecipeComment | ObservedPreference] = []
    for entry in entries_by_user.get(user_id, []):
        metadata = entry["metadata"]
        recipe_name = metadata["recipe_name"]
        created_at = entry["created_at"]
        comment = entry.get("notes", "")
        source_id = f"recipe-comment:{metadata['user_id']}:{recipe_name}"
        activities.append(
            RecipeComment(
                id=source_id,
                user_id=metadata["user_id"],
                recipe_name=recipe_name,
                comment=comment,
                created_at=created_at,
                tags=entry.get("tags", []),
            )
        )

        if "substitut" in comment.lower() or "instead" in comment.lower():
            activities.append(
                ObservedPreference(
                    id=f"observed-preference:{metadata['user_id']}:{recipe_name}",
                    user_id=metadata["user_id"],
                    topic="substitutions",
                    summary=f"{user_id} adapts {recipe_name} based on available ingredients.",
                    evidence=comment,
                    created_at=created_at,
                )
            )
    return activities


@enzyme.transform(client)
def recipe_collection(recipe: RecipeComment | ObservedPreference) -> Activity:
    if isinstance(recipe, RecipeComment):
        return Activity(
            title=recipe.recipe_name,
            content=recipe.comment,
            created_at=recipe.created_at,
            tags=recipe.tags,
            source_id=recipe.id,
            collection="recipe/comments",
            metadata={
                "activity_type": "recipe_comment",
                "user_id": recipe.user_id,
                "recipe_name": recipe.recipe_name,
            },
        )

    return Activity(
        title=f"Observed preference: {recipe.topic}",
        content=f"{recipe.summary}\n\nEvidence: {recipe.evidence}",
        created_at=recipe.created_at,
        tags=[recipe.topic, "observed-preference"],
        source_id=recipe.id,
        collection="agent/observed-preferences",
        metadata={
            "activity_type": "observed_preference",
            "user_id": recipe.user_id,
            "topic": recipe.topic,
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Enzyme MCP server with NYT recipe data"
    )
    parser.add_argument("--port", type=int, default=9460)
    parser.add_argument("--ngrok", action="store_true", help="Expose via ngrok tunnel")
    parser.add_argument(
        "--ngrok-domain", type=str, default=None, help="Custom ngrok domain"
    )
    args = parser.parse_args()

    client.serve(
        port=args.port,
        init_users=sorted(entries_by_user.keys()),
        ngrok=args.ngrok,
        ngrok_domain=args.ngrok_domain,
    )
