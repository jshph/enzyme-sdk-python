"""Typed ingest payloads for connector transforms."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


ActivityCollection = type


class CatalystProfile(str, Enum):
    PREFERENCE_EVIDENCE = "preference_evidence"
    TENSION_TRACE = "tension_trace"
    RESONANCE_TRACE = "resonance_trace"
    RELATIONAL = "relational"
    OPERATIONAL = "operational"
    DECISION_TRACE = "decision_trace"
    REFLECTIVE = "reflective"


@dataclass
class Activity:
    """Canonical Enzyme activity emitted by ``@enzyme.transform``.

    Connector hydrators and save hooks can return app-native objects. A
    transform converts those objects into this payload before ingest.
    """

    title: str
    content: str
    created_at: str | int | float | None = None
    source_id: str | None = None
    collections: list[type] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "title": str(self.title),
            "content": _entry_content(self.content, self.metadata),
        }
        if self.created_at is not None:
            entry["created_at"] = self.created_at
        if self.source_id:
            entry["id"] = self.source_id
            entry["source_id"] = self.source_id
        if self.collections:
            entry["collections"] = [collection_id(collection) for collection in self.collections]
        return entry


def _entry_content(content: str, metadata: dict[str, Any]) -> str:
    body = str(content)
    if not metadata:
        return body
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    return f"{body}\n\nMetadata: {metadata_json}"


def collection_id(collection: type) -> str:
    name = getattr(collection, "__name__", str(collection))
    chars: list[str] = []
    for index, char in enumerate(name):
        if char.isupper() and index > 0 and name[index - 1].isalnum():
            chars.append("-")
        chars.append(char.lower() if char.isalnum() else "-")
    slug = "-".join(part for part in "".join(chars).split("-") if part)
    return slug or "activity"
