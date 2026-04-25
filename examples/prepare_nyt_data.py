"""Extract a single power-user's NYT Cooking comment history into enzyme ingest format.

Reads the Kaggle CSV, picks one prolific commenter, derives tags from recipe names,
and writes a JSON file ready for enzyme ingest.
"""

import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone

ZIP_PATH = "/Users/joshuapham/Downloads/nyt_recipe_comments_Jun25.csv.zip"
TARGET_UID = "38686708.0"  # dimmerswitch — 352 comments, 318 recipes
OUTPUT_PATH = "examples/nyt_user_data.json"

# ── Tag derivation from recipe names ─────────────────────────────────────────
# Since the dataset has no tags, we derive them from recipe name keywords.
# These represent real cooking categories a user would recognize.

PROTEIN_TAGS = {
    "chicken": "chicken", "turkey": "turkey", "pork": "pork", "beef": "beef",
    "lamb": "lamb", "salmon": "salmon", "shrimp": "shrimp", "fish": "fish",
    "crab": "crab", "lobster": "lobster", "clam": "clams", "mussel": "mussels",
    "scallop": "scallops", "sausage": "sausage", "meatball": "meatballs",
    "steak": "steak", "oxtail": "oxtail", "duck": "duck", "egg": "eggs",
    "tofu": "tofu", "lentil": "lentils", "chickpea": "chickpeas",
    "bean": "beans", "meatloaf": "meatloaf",
}

CUISINE_TAGS = {
    "italian": "italian", "pasta": "italian", "risotto": "italian",
    "gnocchi": "italian", "lasagna": "italian", "bolognese": "italian",
    "parmigiana": "italian", "marinara": "italian", "ziti": "italian",
    "macaroni": "italian", "linguine": "italian", "spaghetti": "italian",
    "japanese": "japanese", "miso": "japanese", "soba": "japanese",
    "teriyaki": "japanese", "ramen": "japanese",
    "korean": "korean", "kimchi": "korean", "gochujang": "korean",
    "thai": "thai", "curry": "thai", "coconut": "southeast-asian",
    "mexican": "mexican", "taco": "mexican", "enchilada": "mexican",
    "salsa": "mexican", "tortilla": "mexican",
    "indian": "indian", "dal": "indian", "tandoori": "indian",
    "masala": "indian", "tikka": "indian", "biryani": "indian",
    "french": "french", "provenc": "french", "bourguignon": "french",
    "cassoulet": "french", "clafoutis": "french", "gratin": "french",
    "mediterranean": "mediterranean", "greek": "mediterranean",
    "shakshuka": "mediterranean", "falafel": "mediterranean",
    "chinese": "chinese", "stir fry": "chinese", "wok": "chinese",
    "sicilian": "italian", "tuscan": "italian",
    "middle eastern": "middle-eastern", "shawarma": "middle-eastern",
    "hummus": "middle-eastern", "tahini": "middle-eastern",
}

TECHNIQUE_TAGS = {
    "roast": "roasting", "bake": "baking", "braise": "braising",
    "grill": "grilling", "fry": "frying", "stew": "stew",
    "soup": "soup", "salad": "salad", "slow cook": "slow-cooking",
    "smok": "smoking", "pickle": "pickling", "ferment": "fermentation",
    "saut": "sauteing",
}

CATEGORY_TAGS = {
    "cake": "baking", "cookie": "baking", "bread": "baking",
    "pie": "baking", "tart": "baking", "muffin": "baking",
    "biscuit": "baking", "scone": "baking", "brownie": "baking",
    "dessert": "dessert", "sundae": "dessert", "pudding": "dessert",
    "ice cream": "dessert", "chocolate": "dessert",
    "cocktail": "drinks", "margarita": "drinks",
    "sandwich": "sandwich", "toast": "sandwich",
    "pizza": "pizza", "flatbread": "pizza",
    "breakfast": "breakfast", "pancake": "breakfast", "waffle": "breakfast",
    "frittata": "breakfast",
    "pasta": "pasta", "noodle": "noodles",
    "vegetarian": "vegetarian", "vegan": "vegan",
    "weeknight": "weeknight",
}

VEGGIE_TAGS = {
    "cauliflower": "cauliflower", "brussels": "brussels-sprouts",
    "eggplant": "eggplant", "mushroom": "mushrooms", "potato": "potatoes",
    "tomato": "tomatoes", "squash": "squash", "zucchini": "zucchini",
    "corn": "corn", "carrot": "carrots", "cabbage": "cabbage",
    "kale": "greens", "spinach": "greens", "chard": "greens",
    "broccoli": "broccoli", "onion": "alliums", "shallot": "alliums",
    "garlic": "alliums", "leek": "alliums",
}

# Broader ingredient/flavor tags derived from comment text too
COMMENT_SIGNAL_TAGS = {
    "olive oil": "olive-oil", "butter": "butter", "cream": "dairy",
    "parmesan": "cheese", "parm": "cheese", "cheddar": "cheese",
    "gruyere": "cheese", "feta": "cheese", "mozzarella": "cheese",
    "ricotta": "cheese", "cheese": "cheese",
    "lemon": "citrus", "lime": "citrus", "orange": "citrus",
    "cumin": "spices", "coriander": "spices", "paprika": "spices",
    "cinnamon": "warm-spices", "nutmeg": "warm-spices",
    "chili": "heat", "chipotle": "heat", "jalapeno": "heat",
    "sriracha": "heat", "hot sauce": "heat", "cayenne": "heat",
    "ginger": "ginger", "soy sauce": "umami", "miso": "umami",
    "fish sauce": "umami", "anchov": "umami", "worcestershire": "umami",
    "wine": "wine-cooking", "beer": "beer-cooking",
    "coconut milk": "coconut", "coconut cream": "coconut",
    "yogurt": "dairy", "sour cream": "dairy",
    "cast iron": "cast-iron", "dutch oven": "dutch-oven",
    "instant pot": "pressure-cooking", "pressure cook": "pressure-cooking",
    "make ahead": "make-ahead", "make-ahead": "make-ahead",
    "leftover": "leftovers",
}


def derive_tags(recipe_name: str, comment_text: str = "") -> list[str]:
    """Derive tags from recipe name + comment text using keyword matching."""
    name = recipe_name.lower()
    text = (name + " " + comment_text).lower()
    tags = set()

    # Recipe name tags
    for keyword, tag in PROTEIN_TAGS.items():
        if keyword in name:
            tags.add(tag)

    for keyword, tag in CUISINE_TAGS.items():
        if keyword in name:
            tags.add(tag)

    for keyword, tag in TECHNIQUE_TAGS.items():
        if keyword in name:
            tags.add(tag)

    for keyword, tag in CATEGORY_TAGS.items():
        if keyword in name:
            tags.add(tag)

    for keyword, tag in VEGGIE_TAGS.items():
        if keyword in name:
            tags.add(tag)

    # Comment text tags (ingredient/technique signals from user's own words)
    for keyword, tag in COMMENT_SIGNAL_TAGS.items():
        if keyword in text:
            tags.add(tag)

    # If no protein/tofu/legume found, and has veggie tags, mark vegetarian
    has_protein = tags & set(PROTEIN_TAGS.values())
    has_veggie = tags & set(VEGGIE_TAGS.values())
    if not has_protein and has_veggie:
        tags.add("vegetarian")

    return sorted(tags)


def derive_folder(tags: list[str], recipe_name: str) -> str:
    """Derive a folder grouping from tags — represents behavioral clusters."""
    name = recipe_name.lower()

    if any(t in tags for t in ["baking", "dessert"]):
        return "baking-and-desserts"
    if any(t in tags for t in ["pasta", "noodles", "italian"]):
        return "pasta-and-noodles"
    if any(t in tags for t in ["soup", "stew"]):
        return "soups-and-stews"
    if any(t in tags for t in ["salad"]):
        return "salads"
    if any(t in tags for t in ["breakfast", "sandwich"]):
        return "breakfast-and-light"
    if any(t in tags for t in ["grilling"]):
        return "grilling"
    if "side" in name or "vegetable" in name:
        return "sides"

    return "mains"


def clean_comment(body: str) -> str:
    """Clean HTML artifacts from comment body."""
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    body = body.replace("&quot;", '"').replace("&#39;", "'")
    return body.strip()


def main():
    zf = zipfile.ZipFile(ZIP_PATH)
    with zf.open("nyt_recipe_comments_Jun25.csv") as f:
        reader = csv.DictReader(io.TextIOWrapper(f))

        comments = []
        for row in reader:
            if row["userID"] == TARGET_UID:
                body = clean_comment(row["commentBody"])
                if len(body) > 50:  # substantive only
                    comments.append(row)

    print(f"Found {len(comments)} substantive comments from {comments[0]['userDisplayName']}")

    # Convert to enzyme ingest entries
    entries = []
    seen_recipes = set()

    for row in comments:
        recipe = row["recipe_name"]

        # For recipes with multiple comments, merge them
        if recipe in seen_recipes:
            for entry in entries:
                if entry["_recipe"] == recipe:
                    new_body = clean_comment(row["commentBody"])
                    entry["notes"] += "\n\n" + new_body
                    # Re-derive tags with merged text
                    entry["tags"] = derive_tags(recipe, entry["notes"])
                    entry["folder"] = derive_folder(entry["tags"], recipe)
                    # Use the latest date
                    ts = float(row["approveDate"])
                    entry_ts = entry.get("_ts", 0)
                    if ts > entry_ts:
                        entry["_ts"] = ts
                        entry["created_at"] = datetime.fromtimestamp(
                            ts, tz=timezone.utc
                        ).strftime("%Y-%m-%d")
                    break
            continue

        seen_recipes.add(recipe)
        tags = derive_tags(recipe, body)
        folder = derive_folder(tags, recipe)
        ts = float(row["approveDate"])
        body = clean_comment(row["commentBody"])

        # Title is the recipe name, title-cased
        title = recipe.replace("-", " ").title()

        entries.append(
            {
                "title": title,
                "content": f"NYT Cooking recipe: {title}",
                "notes": body,
                "tags": tags,
                "folder": folder,
                "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                    "%Y-%m-%d"
                ),
                "_recipe": recipe,  # internal, stripped before output
                "_ts": ts,
            }
        )

    # Sort by date
    entries.sort(key=lambda e: e["created_at"])

    # Strip internal fields
    for entry in entries:
        del entry["_recipe"]
        del entry["_ts"]

    # Stats
    all_tags = defaultdict(int)
    all_folders = defaultdict(int)
    for e in entries:
        for t in e["tags"]:
            all_tags[t] += 1
        all_folders[e["folder"]] += 1

    print(f"Created {len(entries)} unique recipe entries")
    print(f"Date range: {entries[0]['created_at']} to {entries[-1]['created_at']}")
    print()
    print(f"Folders ({len(all_folders)}):")
    for folder, count in sorted(all_folders.items(), key=lambda x: -x[1]):
        print(f"  {count:4d}  {folder}")
    print()
    print(f"Top tags ({len(all_tags)} total):")
    for tag, count in sorted(all_tags.items(), key=lambda x: -x[1])[:20]:
        print(f"  {count:4d}  {tag}")

    # Write output
    with open(OUTPUT_PATH, "w") as f:
        json.dump({"entries": entries}, f, indent=2)
    print(f"\nWritten to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
