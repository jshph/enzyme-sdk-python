"""Storage backend — manages per-user vault directories on disk."""

from __future__ import annotations

import shutil
from pathlib import Path

from enzyme_sdk.document import Document

DEFAULT_BASE_PATH = Path.home() / ".enzyme-sdk" / "collections"


class VaultStore:
    """Manages per-collection vault directories on the filesystem.

    Each collection gets its own directory under the base path,
    which serves as an Enzyme vault. Documents are written as
    markdown files within the vault.
    """

    def __init__(self, base_path: Path | str | None = None):
        self.base_path = Path(base_path) if base_path else DEFAULT_BASE_PATH
        self.base_path.mkdir(parents=True, exist_ok=True)

    def vault_path(self, collection_id: str) -> Path:
        """Get the vault directory path for a collection."""
        return self.base_path / collection_id

    def create_vault(self, collection_id: str) -> Path:
        """Create a new vault directory for a collection."""
        path = self.vault_path(collection_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def delete_vault(self, collection_id: str) -> None:
        """Delete a collection's vault directory."""
        path = self.vault_path(collection_id)
        if path.exists():
            shutil.rmtree(path)

    def vault_exists(self, collection_id: str) -> bool:
        """Check if a collection's vault exists."""
        return self.vault_path(collection_id).exists()

    def list_vaults(self) -> list[str]:
        """List all collection IDs."""
        if not self.base_path.exists():
            return []
        return [p.name for p in self.base_path.iterdir() if p.is_dir()]

    def write_document(self, collection_id: str, document: Document) -> Path:
        """Write a document as a markdown file in the collection's vault."""
        vault = self.vault_path(collection_id)
        if not vault.exists():
            raise FileNotFoundError(f"Collection vault does not exist: {collection_id}")

        filepath = vault / document.filename()
        filepath.write_text(document.to_markdown(), encoding="utf-8")
        return filepath

    def delete_document(self, collection_id: str, filename: str) -> None:
        """Delete a document from a collection's vault."""
        filepath = self.vault_path(collection_id) / filename
        if filepath.exists():
            filepath.unlink()

    def list_documents(self, collection_id: str) -> list[str]:
        """List document filenames in a collection."""
        vault = self.vault_path(collection_id)
        if not vault.exists():
            return []
        return [f.name for f in vault.glob("*.md")]

    def read_document(self, collection_id: str, filename: str) -> str | None:
        """Read a document's raw content."""
        filepath = self.vault_path(collection_id) / filename
        if filepath.exists():
            return filepath.read_text(encoding="utf-8")
        return None
