"""Typed ingest payloads for connector transforms."""

from __future__ import annotations

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
    tags: list[str] = field(default_factory=list)
    created_at: str | int | float | None = None
    source_id: str | None = None
    collection: str | None = None
    collections: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "title": self.title,
            "content": self.content,
        }
        if self.tags:
            entry["tags"] = list(self.tags)
        if self.created_at is not None:
            entry["created_at"] = self.created_at
        if self.source_id:
            entry["source_id"] = self.source_id
        if self.collection:
            entry["collection"] = self.collection
        if self.collections:
            entry["collections"] = list(self.collections)
        if self.metadata:
            entry["metadata"] = dict(self.metadata)
        return entry
