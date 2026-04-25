"""Prepare NYT recipe comments for enzyme ingest.

Defaults to the bundled three-user sample and writes one prepared JSON to the
system temp directory. The output includes all sample users for cluster-label
building plus one selected user's entries for ingest.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import tempfile
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "nyt_sample_comments.json"

USERS = {
    "dimmerswitch": "38686708.0",
    "es": "1942514.0",
    "christa": "1570140.0",
    "luther": "66479981.0",
}


def clean_comment(body: str) -> str:
    body = re.sub(r"<br\s*/?>", "\n", body)
    body = re.sub(r"<[^>]+>", "", body)
    body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    body = body.replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", body).strip()


def title_from_recipe(recipe_name: str) -> str:
    return recipe_name.replace("-", " ").title()


def parse_date(row: dict[str, Any]) -> str:
    if row.get("date"):
        return str(row["date"])[:10]
    if row.get("approveDate"):
        return datetime.fromtimestamp(
            float(row["approveDate"]), tz=timezone.utc
        ).strftime("%Y-%m-%d")
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    recipe_name = row.get("recipe_name") or row.get("recipeName")
    comment = row.get("comment") or row.get("commentBody")
    user_id = str(row.get("user_id") or row.get("userID") or "")
    user_key = row.get("user_key") or row.get("userDisplayName") or user_id

    return {
        "user_key": str(user_key),
        "user_id": user_id,
        "recipe_name": str(recipe_name or "").strip(),
        "comment": clean_comment(str(comment or "")),
        "date": parse_date(row),
    }


def load_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        return [normalize_row(row) for row in data["comments"]]

    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as zf:
            csv_name = next(name for name in zf.namelist() if name.endswith(".csv"))
            with zf.open(csv_name) as f:
                return [
                    normalize_row(row)
                    for row in csv.DictReader(io.TextIOWrapper(f))
                ]

    with path.open(newline="", encoding="utf-8") as f:
        return [normalize_row(row) for row in csv.DictReader(f)]


def rows_to_entries(
    rows: list[dict[str, Any]],
    *,
    include_user_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    entries_by_recipe: dict[str, dict[str, Any]] = {}

    for row in rows:
        if include_user_keys is not None and row["user_key"] not in include_user_keys:
            continue
        if not row["recipe_name"] or len(row["comment"]) <= 50:
            continue

        key = f"{row['user_id']}::{row['recipe_name']}"
        if key in entries_by_recipe:
            entry = entries_by_recipe[key]
            entry["notes"] += "\n\n" + row["comment"]
            entry["created_at"] = max(entry["created_at"], row["date"])
            continue

        recipe = row["recipe_name"]
        title = title_from_recipe(recipe)
        entries_by_recipe[key] = {
            "title": title,
            "content": f"NYT Cooking recipe: {title}",
            "notes": row["comment"],
            "created_at": row["date"],
            "metadata": {
                "user_key": row["user_key"],
                "user_id": row["user_id"],
                "recipe_name": recipe,
            },
        }

    return sorted(
        entries_by_recipe.values(),
        key=lambda entry: (
            entry["metadata"]["user_key"],
            entry["created_at"],
            entry["metadata"]["recipe_name"],
        ),
    )


def default_output(user: str) -> Path:
    return Path(tempfile.gettempdir()) / f"nyt_{user}_data.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("user", nargs="?", default="es")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows = load_rows(args.input)
    sample_user_keys = set(USERS)
    cluster_entries = rows_to_entries(rows, include_user_keys=sample_user_keys)
    target_user_id = USERS.get(args.user, args.user)
    entries = [
        entry for entry in cluster_entries
        if entry["metadata"]["user_key"] == args.user
        or entry["metadata"]["user_id"] == target_user_id
    ]
    if not entries:
        raise SystemExit(f"No entries found for user {args.user!r}")

    output = args.output or default_output(args.user)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "cluster_entries": cluster_entries,
                "entries": entries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    counts = defaultdict(int)
    for row in rows:
        if row["user_key"] in USERS or row["user_id"] in USERS.values():
            counts[row["user_key"]] += 1

    print(f"Loaded {len(rows)} comments from {args.input}")
    print(f"Sample users: {dict(sorted(counts.items()))}")
    print(f"Prepared {len(cluster_entries)} cluster entries across sample users")
    print(f"Wrote {len(entries)} entries for {args.user!r} to {output}")


if __name__ == "__main__":
    main()
