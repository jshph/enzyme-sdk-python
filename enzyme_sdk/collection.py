"""Collection — a user's document collection backed by an Enzyme vault."""

from __future__ import annotations

from pathlib import Path

from enzyme_sdk.client import CatalyzeResponse, EnzymeClient, PetriResponse, VaultStatus
from enzyme_sdk.document import Document
from enzyme_sdk.store import VaultStore

# Re-export for convenience
from enzyme_sdk.client import CatalyzeResult, ContributingCatalyst, PetriEntity  # noqa: F401


class Collection:
    """A collection of documents indexed by Enzyme.

    Each collection maps to a directory of markdown files on disk.
    The SDK manages writing documents into the directory; Enzyme
    handles the indexing and search.

    Bootstrap on an existing vault:

        coll = Collection("reading", client=client, vault_path="~/vault/Readwise")
        results = coll.search("editorial taste and deliberate curation")

    Create a fresh collection:

        coll = Collection("user-123", client=client)
        coll.create()
        coll.add(doc, folder="Sessions")
        coll.refresh()
    """

    def __init__(
        self,
        collection_id: str,
        *,
        client: EnzymeClient | None = None,
        store: VaultStore | None = None,
        vault_path: str | Path | None = None,
        use_collection_flag: bool = True,
    ):
        self.collection_id = collection_id
        self.client = client or EnzymeClient()
        self._store = store or VaultStore()
        self._vault_override = Path(vault_path).expanduser() if vault_path else None
        self._use_collection_flag = use_collection_flag and not vault_path

    @property
    def vault_path(self) -> Path:
        if self._vault_override:
            return self._vault_override
        return self._store.vault_path(self.collection_id)

    @property
    def _collection_or_vault(self) -> dict:
        """Return kwargs for client calls — either collection= or vault=."""
        if self._use_collection_flag:
            return {"collection": self.collection_id}
        return {"vault": str(self.vault_path)}

    def create(self) -> "Collection":
        """Create the collection directory. Returns self for chaining."""
        if self._vault_override:
            self._vault_override.mkdir(parents=True, exist_ok=True)
        else:
            self._store.create_vault(self.collection_id)
        return self

    def delete(self) -> None:
        """Delete this collection and all its data."""
        import shutil
        path = self.vault_path
        if path.exists():
            shutil.rmtree(path)

    def add(self, document: Document, *, folder: str | None = None) -> Path:
        """Add a document to the collection.

        Writes the document as markdown. Call `refresh()` after adding
        documents to update the index.

        Args:
            document: The document to add.
            folder: Subfolder to write into (e.g., "Sessions/2026").
                    Created if it doesn't exist.
        """
        target = self.vault_path / folder if folder else self.vault_path
        target.mkdir(parents=True, exist_ok=True)
        filepath = target / document.filename()
        filepath.write_text(document.to_markdown(), encoding="utf-8")
        return filepath

    def add_many(self, documents: list[Document], *, folder: str | None = None) -> list[Path]:
        """Add multiple documents."""
        return [self.add(doc, folder=folder) for doc in documents]

    def ingest(self, entry: dict) -> dict:
        """Ingest a single entry directly into the DB (no filesystem writes).

        This is the streaming path — call it for each new user action.
        The entry is chunked, hashed, and indexed immediately.

        Args:
            entry: Document dict with at least 'title'. See
                   EnzymeClient.ingest() for full schema.
        """
        return self.client.ingest(**self._collection_or_vault, entry=entry)

    def ingest_many(self, entries: list[dict]) -> dict:
        """Batch ingest entries directly into the DB (no filesystem writes).

        This is the batch import path — call it at signup to import
        the user's existing collection.

        Args:
            entries: List of document dicts. See EnzymeClient.ingest()
                     for full schema.
        """
        return self.client.ingest(**self._collection_or_vault, entries=entries)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        register: str = "explore",
    ) -> CatalyzeResponse:
        """Search the collection by concept.

        Queries don't need to match document text — Enzyme routes them
        through precomputed thematic questions that characterize the
        content. A broad query returns results ranked by conceptual
        relevance, with the questions that drove each match.

        Args:
            query: What you're looking for, in natural language.
            limit: Max results.
            register: "explore" (open-ended), "continuity" (follow-up),
                      "reference" (precise lookup).
        """
        return self.client.catalyze(query, **self._collection_or_vault, limit=limit, register=register)

    def overview(self, *, top: int | None = None, query: str | None = None) -> PetriResponse:
        """See what the index understands about this collection.

        Returns entities (tags, folders, links) and the thematic questions
        generated for each. This is the conceptual structure Enzyme built
        from the documents — the retrieval paths that power search.

        Args:
            top: Number of top entities to return.
            query: Rank entities by relevance to this query.
        """
        return self.client.petri(**self._collection_or_vault, top=top, query=query)

    def refresh(self, *, full: bool = False) -> None:
        """Update the index after adding or modifying documents."""
        self.client.refresh(**self._collection_or_vault, full=full)

    def initialize(self) -> None:
        """Run the full indexing pipeline on a new collection."""
        self.client.init(**self._collection_or_vault)

    def status(self) -> VaultStatus:
        """Check index health."""
        return self.client.status(**self._collection_or_vault)

    def list_documents(self) -> list[str]:
        """List all markdown filenames in the collection."""
        return sorted(f.name for f in self.vault_path.rglob("*.md")
                      if ".enzyme" not in f.parts)

    def list_folders(self) -> list[str]:
        """List subdirectories that contain documents."""
        folders = set()
        for md in self.vault_path.rglob("*.md"):
            if ".enzyme" in md.parts:
                continue
            rel = md.relative_to(self.vault_path).parent
            if rel != Path("."):
                folders.add(str(rel))
        return sorted(folders)

    @property
    def is_indexed(self) -> bool:
        """Whether the collection has been initialized with Enzyme."""
        return (self.vault_path / ".enzyme").exists()

    @classmethod
    def open(
        cls,
        path: str | Path,
        *,
        client: EnzymeClient | None = None,
        auto_init: bool = False,
    ) -> "Collection":
        """Open an existing directory as a collection.

        The simplest way to start — point at a folder of content and go.
        If the folder hasn't been indexed yet, set `auto_init=True` to
        run the full pipeline (takes a few minutes on first run).

            coll = Collection.open("~/vault/content/Readwise")
            results = coll.search("editorial taste")

        Args:
            path: Directory containing markdown files.
            client: EnzymeClient instance (uses default if not provided).
            auto_init: Run `enzyme init` if not already indexed.
        """
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"Directory not found: {resolved}")

        coll = cls(resolved.name, client=client, vault_path=resolved)

        if auto_init and not coll.is_indexed:
            coll.initialize()

        return coll
