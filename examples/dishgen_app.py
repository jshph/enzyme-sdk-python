"""Recipe app with Enzyme integration — backed by real NYT recipe data.

Setup:
    pip install enzyme-sdk fastapi uvicorn
    export ENZYME_API_KEY=enz_...   # from enzyme.garden/settings
    python examples/dishgen_app.py

Then visit http://localhost:8001/docs for the interactive API.
"""

from __future__ import annotations

import sys
import os
import uuid
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from enzyme_sdk.enzyme import enzyme, EnzymeHosted

# --- Load real NYT recipe data ---
from examples.prepare_nyt_data import load_rows, rows_to_entries, DEFAULT_INPUT, USERS

_rows = load_rows(DEFAULT_INPUT)
_all_entries = rows_to_entries(_rows, include_user_keys=set(USERS))

# Group by user
_entries_by_user: dict[str, list[dict]] = {}
for entry in _all_entries:
    uid = entry["metadata"]["user_key"]
    _entries_by_user.setdefault(uid, []).append(entry)

# --- Enzyme integration ---

app_enzyme = EnzymeHosted(
    display_name="NYT Cooking",
    description="Your NYT recipe comments and cooking notes",
    system_prompt=(
        "You are helping a home cook explore their NYT Cooking recipe collection. "
        "Reference specific recipes by name, mention their personal cooking notes, "
        "and highlight patterns you notice across their history."
    ),
)


@enzyme.on_save(app_enzyme, entity="recipe",
    title="title", content="content", tags="tags")
def save_recipe(user_id: str, recipe: dict) -> dict:
    """The decorated function returns the recipe unchanged — enzyme extracts what it needs."""
    return recipe


@enzyme.hydrate(app_enzyme, entity="recipe")
def hydrate_recipes(user_id: str) -> list[dict]:
    """Bulk fetch all recipes for a user (called on connection)."""
    return [
        {
            "title": e["title"],
            "content": e.get("content", "") + "\n\n" + e.get("notes", ""),
            "tags": e.get("tags", []),
        }
        for e in _entries_by_user.get(user_id, [])
    ]


# ---------------------------------------------------------------------------
# In-memory recipe store
# ---------------------------------------------------------------------------


class RecipeStore:
    def __init__(self):
        self._recipes: dict[str, dict[str, dict]] = {}  # user_id -> recipe_id -> recipe

    def seed_from_nyt(self, entries_by_user: dict[str, list[dict]]):
        for user_id, entries in entries_by_user.items():
            self._recipes[user_id] = {}
            for i, entry in enumerate(entries):
                rid = f"nyt-{i}"
                self._recipes[user_id][rid] = {
                    "id": rid,
                    "title": entry["title"],
                    "instructions": entry.get("content", ""),
                    "notes": entry.get("notes", ""),
                    "tags": entry.get("tags", []),
                    "created_at": entry.get("created_at", ""),
                }

    def get_user_recipes(self, user_id: str) -> list[dict]:
        return list(self._recipes.get(user_id, {}).values())

    def get_recipe(self, user_id: str, recipe_id: str) -> dict | None:
        return self._recipes.get(user_id, {}).get(recipe_id)

    def create_recipe(self, user_id: str, recipe: dict) -> dict:
        self._recipes.setdefault(user_id, {})
        recipe_id = recipe.get("id") or str(uuid.uuid4())[:8]
        recipe = {
            "id": recipe_id,
            "title": recipe["title"],
            "instructions": recipe["instructions"],
            "tags": recipe.get("tags", []),
            "rating": recipe.get("rating", 0),
            "notes": recipe.get("notes", ""),
            "created_at": recipe.get("created_at", datetime.now(tz=None).isoformat() + "Z"),
        }
        self._recipes[user_id][recipe_id] = recipe
        return recipe

    def update_recipe(self, user_id: str, recipe_id: str, updates: dict) -> dict | None:
        recipe = self.get_recipe(user_id, recipe_id)
        if recipe is None:
            return None
        recipe.update({k: v for k, v in updates.items() if v is not None})
        return recipe

    def delete_recipe(self, user_id: str, recipe_id: str) -> bool:
        user_recipes = self._recipes.get(user_id, {})
        if recipe_id in user_recipes:
            del user_recipes[recipe_id]
            return True
        return False


db = RecipeStore()
db.seed_from_nyt(_entries_by_user)

# ---------------------------------------------------------------------------
# FastAPI app — CRUD + Enzyme MCP on the same server
# ---------------------------------------------------------------------------

app = FastAPI(title="NYT Cooking", version="0.1.0")

# Mount the MCP endpoint so Claude can talk to the same server
mcp_app = app_enzyme.as_mcp_app()
app.mount("/enzyme", mcp_app)


class RecipeCreate(BaseModel):
    title: str
    instructions: str
    tags: list[str] = []
    rating: int = 0
    notes: str = ""


class RecipeUpdate(BaseModel):
    title: str | None = None
    instructions: str | None = None
    tags: list[str] | None = None
    rating: int | None = None
    notes: str | None = None


@app.get("/health")
def health():
    return {"status": "ok", "recipes_loaded": sum(len(r) for r in db._recipes.values())}


@app.get("/users/{user_id}/recipes")
def list_recipes(user_id: str):
    return db.get_user_recipes(user_id)


@app.get("/users/{user_id}/recipes/{recipe_id}")
def get_recipe(user_id: str, recipe_id: str):
    recipe = db.get_recipe(user_id, recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@app.post("/users/{user_id}/recipes", status_code=201)
def create_recipe(user_id: str, body: RecipeCreate):
    recipe = db.create_recipe(user_id, body.model_dump())
    # Notify Enzyme of the new recipe
    save_recipe(user_id, recipe)
    return recipe


@app.patch("/users/{user_id}/recipes/{recipe_id}")
def update_recipe(user_id: str, recipe_id: str, body: RecipeUpdate):
    recipe = db.update_recipe(user_id, recipe_id, body.model_dump(exclude_unset=True))
    if recipe is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    # Notify Enzyme of the update
    save_recipe(user_id, recipe)
    return recipe


@app.delete("/users/{user_id}/recipes/{recipe_id}", status_code=204)
def delete_recipe(user_id: str, recipe_id: str):
    if not db.delete_recipe(user_id, recipe_id):
        raise HTTPException(status_code=404, detail="Recipe not found")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    print()
    print("  NYT Cooking app running on http://localhost:8001")
    print(f"  Users: {sorted(_entries_by_user.keys())}")
    print(f"  Total recipes: {sum(len(v) for v in _entries_by_user.values())}")
    print("  CRUD API:  http://localhost:8001/docs")
    print("  MCP:       http://localhost:8001/enzyme/mcp")
    print()
    uvicorn.run(app, host="0.0.0.0", port=8001)
