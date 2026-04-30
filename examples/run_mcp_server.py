"""Run a local MCP server backed by Enzyme with real NYT recipe data.

Usage:
    python examples/run_mcp_server.py                    # local only
    python examples/run_mcp_server.py --ngrok             # expose via ngrok
    python examples/run_mcp_server.py --ngrok --port 8080 # custom port
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Iterable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from enzyme_sdk.activity import Activity, CatalystProfile
from enzyme_sdk.enzyme import EnzymeConnector, enzyme
from examples.prepare_nyt_data import DEFAULT_INPUT, USERS, load_rows, rows_to_entries


@dataclass
class UserRecipeComment:
    id: str
    user_id: str
    recipe_name: str
    comment: str
    created_at: str
    tags: list[str]


@dataclass
class AgentObservedPreference:
    id: str
    user_id: str
    topic: str
    summary: str
    source_activity_id: str
    created_at: str


@dataclass
class PreferenceSubstitution:
    pass


@dataclass
class PreferenceRepeatWorthyRecipe:
    pass


@dataclass
class PreferenceSweetnessAdjustment:
    pass


PREFERENCE_COLLECTIONS = {
    "substitutions": PreferenceSubstitution,
    "repeat-worthy recipes": PreferenceRepeatWorthyRecipe,
    "sweetness adjustments": PreferenceSweetnessAdjustment,
}


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
    collections=[
        UserRecipeComment,
        AgentObservedPreference,
        PreferenceSubstitution,
        PreferenceRepeatWorthyRecipe,
        PreferenceSweetnessAdjustment,
    ],
    catalyst_profiles={
        UserRecipeComment: CatalystProfile.PREFERENCE_EVIDENCE,
        AgentObservedPreference: CatalystProfile.PREFERENCE_EVIDENCE,
        PreferenceSubstitution: CatalystProfile.PREFERENCE_EVIDENCE,
        PreferenceRepeatWorthyRecipe: CatalystProfile.PREFERENCE_EVIDENCE,
        PreferenceSweetnessAdjustment: CatalystProfile.PREFERENCE_EVIDENCE,
    },
)


@enzyme.hydrate(client)
def hydrate_recipes(user_id: str) -> Iterable[UserRecipeComment | AgentObservedPreference]:
    activities: list[UserRecipeComment | AgentObservedPreference] = []

    for entry in entries_by_user.get(user_id, []):
        metadata = entry["metadata"]
        recipe_name = metadata["recipe_name"]
        created_at = entry["created_at"]
        comment = entry.get("notes", "")
        source_id = f"recipe-comment:{metadata['user_id']}:{recipe_name}"
        activities.append(
            UserRecipeComment(
                id=source_id,
                user_id=metadata["user_id"],
                recipe_name=recipe_name,
                comment=comment,
                created_at=created_at,
                tags=entry.get("tags", []),
            )
        )

        lowered = comment.lower()
        if "substitut" in lowered or "instead" in lowered:
            activities.append(
                AgentObservedPreference(
                    id=f"observed-preference:{metadata['user_id']}:{recipe_name}:substitutions",
                    user_id=metadata["user_id"],
                    topic="substitutions",
                    summary=f"{user_id} often adapts {recipe_name} around available ingredients.",
                    source_activity_id=source_id,
                    created_at=created_at,
                )
            )
        if "again" in lowered or "keeper" in lowered or "repertoire" in lowered:
            activities.append(
                AgentObservedPreference(
                    id=f"observed-preference:{metadata['user_id']}:{recipe_name}:repeat",
                    user_id=metadata["user_id"],
                    topic="repeat-worthy recipes",
                    summary=f"{user_id} marked {recipe_name} as worth returning to.",
                    source_activity_id=source_id,
                    created_at=created_at,
                )
            )
        if "too sweet" in lowered or "less sugar" in lowered:
            activities.append(
                AgentObservedPreference(
                    id=f"observed-preference:{metadata['user_id']}:{recipe_name}:sweetness",
                    user_id=metadata["user_id"],
                    topic="sweetness adjustments",
                    summary=f"{user_id} tends to reduce sweetness in {recipe_name}.",
                    source_activity_id=source_id,
                    created_at=created_at,
                )
            )
    return activities


@enzyme.transform(client)
def recipe_collection(recipe: UserRecipeComment | AgentObservedPreference) -> Activity:
    if isinstance(recipe, UserRecipeComment):
        return Activity(
            title=recipe.recipe_name,
            content=recipe.comment,
            created_at=recipe.created_at,
            source_id=recipe.id,
            collections=[UserRecipeComment],
            metadata={
                "activity_type": "recipe_comment",
                "recipe_name": recipe.recipe_name,
                "labels": recipe.tags,
            },
        )

    return Activity(
        title=f"Observed preference: {recipe.topic}",
        content=recipe.summary,
        created_at=recipe.created_at,
        source_id=recipe.id,
        collections=[
            AgentObservedPreference,
            PREFERENCE_COLLECTIONS[recipe.topic],
        ],
        metadata={
            "activity_type": "observed_preference",
            "topic": recipe.topic,
            "derived_from": recipe.source_activity_id,
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
