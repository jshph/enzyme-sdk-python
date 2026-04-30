"""Typed ingest payloads for connector transforms."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


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
    collections: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "title": str(self.title),
            "content": _entry_content(self.content, self.metadata),
        }
        if self.created_at is not None:
            entry["created_at"] = self.created_at
        if self.source_id:
            entry["source_id"] = self.source_id
        if self.collections:
            entry["collections"] = list(self.collections)
        return entry


def _entry_content(content: str, metadata: dict[str, Any]) -> str:
    body = str(content)
    if not metadata:
        return body
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    return f"{body}\n\nMetadata: {metadata_json}"
