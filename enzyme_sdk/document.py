"""Document model — content rendered as markdown for Enzyme to index."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Document(BaseModel):
    """A document to add to a collection.

    The SDK renders this as markdown with YAML frontmatter, wikilinks for
    entities, and inline tags. You provide structured data; the client
    handles the formatting that makes Enzyme's indexing work well.

    Example — a design session log:

        Document.from_text(
            "Session — onboarding round 3",
            "Sarah advanced the denser layout. 'People want to feel like "
            "they're making progress.' Rejected the minimal card approach.",
            tags=["layout", "typography"],
            links=["Sarah", "Meridian"],
        )

    Example — a reading highlight:

        Document.from_text(
            "The Creative Act",
            "- Nothing in this book is known to be true. ...",
            tags=["craft", "creativity"],
            links=["Rick Rubin"],
            author="Rick Rubin",
        )
    """

    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None

    def to_markdown(self) -> str:
        """Render as markdown with YAML frontmatter and wikilinks."""
        parts = ["---"]
        parts.append(f"title: \"{self.title}\"")

        dt = self.created_at or datetime.now()
        parts.append(f"date: {dt.strftime('%Y-%m-%d')}")

        if self.tags:
            parts.append("tags:")
            for tag in self.tags:
                parts.append(f"  - {tag.lstrip('#')}")

        for key, value in self.metadata.items():
            if isinstance(value, list):
                parts.append(f"{key}:")
                for item in value:
                    parts.append(f"  - {item}")
            else:
                parts.append(f"{key}: \"{value}\"")

        parts.append("---")
        parts.append("")
        parts.append(f"# {self.title}")
        parts.append("")

        # Inject wikilinks for entity mentions
        body = self.content
        for link in self.links:
            pattern = rf"(?<!\[\[)\b({re.escape(link)})\b(?!\]\])"
            body = re.sub(pattern, rf"[[\1]]", body, count=1)

        parts.append(body)
        parts.append("")
        return "\n".join(parts)

    def filename(self) -> str:
        """Safe filename from the title."""
        safe = re.sub(r"[^a-zA-Z0-9]+", "-", self.title).strip("-").lower()
        return f"{safe}.md"

    @classmethod
    def from_text(
        cls,
        title: str,
        content: str,
        *,
        tags: list[str] | None = None,
        links: list[str] | None = None,
        **metadata: Any,
    ) -> "Document":
        """Create a document from plain text."""
        return cls(
            title=title,
            content=content,
            tags=tags or [],
            links=links or [],
            metadata=metadata,
        )
