"""FastAPI server — REST API for multi-tenant Enzyme collections."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from enzyme_sdk.client import EnzymeClient, EnzymeError
from enzyme_sdk.collection import Collection
from enzyme_sdk.document import Document
from enzyme_sdk.store import VaultStore

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

API_KEY = os.environ.get("ENZYME_SDK_API_KEY", "dev-key-change-me")
COLLECTIONS_BASE = os.environ.get("ENZYME_SDK_COLLECTIONS_PATH", None)

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

_client = EnzymeClient()
_store = VaultStore(base_path=COLLECTIONS_BASE) if COLLECTIONS_BASE else VaultStore()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def verify_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> str:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return x_api_key


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateCollectionRequest(BaseModel):
    collection_id: str = Field(description="Unique collection identifier (e.g. user ID)")
    description: str | None = Field(default=None, description="Optional description")


class CreateCollectionResponse(BaseModel):
    collection_id: str
    vault_path: str
    message: str


class IngestDocumentRequest(BaseModel):
    title: str
    content: str
    tags: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class IngestDocumentResponse(BaseModel):
    filename: str
    vault_path: str


class QueryRequest(BaseModel):
    model_config = {"populate_by_name": True}

    query: str = Field(description="Conceptual search query")
    limit: int = Field(default=10, ge=1, le=100)
    presentation_register: str = Field(default="explore", alias="register")


class QueryResultItem(BaseModel):
    file_path: str
    similarity: float
    content: str


class QueryResponse(BaseModel):
    query: str
    results: list[QueryResultItem]
    top_contributing_catalysts: list[dict[str, Any]]
    processing_time: float
    total_results: int


class StatusResponse(BaseModel):
    vault_path: str
    documents: int
    embedded: str
    entities: int
    catalysts: int
    model: str
    api_key_configured: bool


class RefreshResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_collection(collection_id: str) -> Collection:
    if not _store.vault_exists(collection_id):
        raise HTTPException(status_code=404, detail=f"Collection '{collection_id}' not found")
    return Collection(collection_id, client=_client, store=_store)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Enzyme SDK API",
    description="Multi-tenant hosted service prototype for Enzyme — thematic document retrieval.",
    version="0.1.0",
)


@app.post("/collections", response_model=CreateCollectionResponse)
def create_collection(
    req: CreateCollectionRequest,
    _key: str = Depends(verify_api_key),
):
    """Create a new collection (backed by an Enzyme vault on disk)."""
    if _store.vault_exists(req.collection_id):
        raise HTTPException(status_code=409, detail=f"Collection '{req.collection_id}' already exists")

    _store.create_vault(req.collection_id)
    vault = _store.vault_path(req.collection_id)

    return CreateCollectionResponse(
        collection_id=req.collection_id,
        vault_path=str(vault),
        message="Collection created. Add documents and call /refresh to build the index.",
    )


@app.get("/collections", response_model=list[str])
def list_collections(_key: str = Depends(verify_api_key)):
    """List all collection IDs."""
    return _store.list_vaults()


@app.post("/collections/{collection_id}/documents", response_model=IngestDocumentResponse)
def ingest_document(
    collection_id: str,
    req: IngestDocumentRequest,
    _key: str = Depends(verify_api_key),
):
    """Ingest a document into a collection."""
    coll = _get_collection(collection_id)
    doc = Document(
        title=req.title,
        content=req.content,
        tags=req.tags,
        links=req.links,
        metadata=req.metadata,
    )
    filepath = coll.add(doc)
    return IngestDocumentResponse(filename=doc.filename(), vault_path=str(filepath))


@app.get("/collections/{collection_id}/documents", response_model=list[str])
def list_documents(
    collection_id: str,
    _key: str = Depends(verify_api_key),
):
    """List document filenames in a collection."""
    coll = _get_collection(collection_id)
    return coll.list_documents()


@app.post("/collections/{collection_id}/query", response_model=QueryResponse)
def query_collection(
    collection_id: str,
    req: QueryRequest,
    _key: str = Depends(verify_api_key),
):
    """Semantic search across a collection using enzyme catalyze."""
    coll = _get_collection(collection_id)
    try:
        resp = coll.search(req.query, limit=req.limit, register=req.presentation_register)
    except EnzymeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    results = [
        QueryResultItem(
            file_path=r.file_path,
            similarity=r.similarity,
            content=r.content,
        )
        for r in resp.results
    ]

    catalysts = [
        {"text": c.text, "entity": c.entity, "relevance_score": c.relevance_score}
        for c in resp.top_contributing_catalysts
    ]

    return QueryResponse(
        query=resp.query,
        results=results,
        top_contributing_catalysts=catalysts,
        processing_time=resp.processing_time,
        total_results=resp.total_results,
    )


@app.post("/collections/{collection_id}/refresh", response_model=RefreshResponse)
def refresh_collection(
    collection_id: str,
    full: bool = False,
    _key: str = Depends(verify_api_key),
):
    """Trigger a re-index of the collection."""
    coll = _get_collection(collection_id)
    try:
        coll.refresh(full=full)
    except EnzymeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return RefreshResponse(message=f"Collection '{collection_id}' refreshed successfully.")


@app.get("/collections/{collection_id}/status", response_model=StatusResponse)
def collection_status(
    collection_id: str,
    _key: str = Depends(verify_api_key),
):
    """Get the index status of a collection."""
    coll = _get_collection(collection_id)
    try:
        st = coll.status()
    except EnzymeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return StatusResponse(
        vault_path=st.vault_path,
        documents=st.documents,
        embedded=st.embedded,
        entities=st.entities,
        catalysts=st.catalysts,
        model=st.model,
        api_key_configured=st.api_key_configured,
    )


@app.delete("/collections/{collection_id}")
def delete_collection(
    collection_id: str,
    _key: str = Depends(verify_api_key),
):
    """Delete a collection and all its data."""
    if not _store.vault_exists(collection_id):
        raise HTTPException(status_code=404, detail=f"Collection '{collection_id}' not found")
    _store.delete_vault(collection_id)
    return {"message": f"Collection '{collection_id}' deleted."}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Run the server with uvicorn."""
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8420)


if __name__ == "__main__":
    main()
