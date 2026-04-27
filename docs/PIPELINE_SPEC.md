# Enzyme SDK Pipeline Spec: CLI Requirements for Hosted Flow

*April 26, 2026 — authored from live debugging session*

## Context

The enzyme SDK (`enzyme_sdk/enzyme.py`) provides a decorator-based integration for app developers. When a user connects, the SDK:

1. Calls `@enzyme.hydrate` to load the user's data
2. Ingests entries into the enzyme DB via `enzyme ingest`
3. Runs the indexing pipeline (embed + catalyze) so `enzyme catalyze` and `enzyme petri` return results

This document specifies what the SDK needs from the CLI binary and where the current gaps are.

## Current State (What Works)

### `enzyme ingest` (via stdin JSON)
- Parses entries → `ProcessedDocument` with chunks
- `db.upsert_documents_batch` — writes docs + chunks to SQLite
- `IndexingPipeline::collect_entity_entries` → `db.upsert_entities_batch` — extracts tag/link/folder entities
- **Does NOT embed.** Documents end up with `embedded: 0/N`.
- Works correctly. No changes needed.

### `enzyme catalyze` / `enzyme petri`
- Query the embedded + catalyzed index
- Work correctly when the index is fully built
- Return empty results when embeddings or catalysts are missing

### `enzyme init`
- Calls `reset_vault_database` — **wipes the DB**
- `IndexingPipeline::run()` scans the filesystem (`FileDiscovery`) for markdown files
- If no files are found and docs exist in DB, returns early with zero work
- Then embeds, selects entities, generates catalysts
- **Problem: wipes DB-ingested entries because it resets the database, then scans files and finds nothing**

### `enzyme refresh`
- Also runs `IndexingPipeline::run()` which scans the filesystem
- Does not wipe the DB, but does not process DB-ingested entries either
- If `all_paths.is_empty()` and `db_doc_count > 0`, returns early (lines 52-62 of `indexing.rs`)
- **Problem: silently skips DB-ingested entries that need embedding**

## The Gap

After `enzyme ingest`, there is no CLI command that:
1. Embeds the DB-stored documents (which have chunks but no embeddings)
2. Generates catalysts from the newly embedded content
3. Does so **without scanning the filesystem for markdown files**

This means the SDK's `ingest` → `init`/`refresh` pipeline doesn't produce a searchable index.

## Required Changes

### 1. `enzyme refresh` should process DB-ingested entries

**Location:** `crates/enzyme-core/src/pipeline/indexing.rs`, `IndexingPipeline::run()`

Currently (lines 48-62):
```rust
let discovery = FileDiscovery::new(self.vault_path.clone());
let all_paths = discovery.discover()?;
if all_paths.is_empty() && db_doc_count > 0 {
    // Returns early — does nothing
    return Ok(IndexingStats { ... });
}
```

**Required behavior:** When there are no files on disk but there ARE documents in the DB that lack embeddings (`embedded: 0/N`), the pipeline should:
- Skip file discovery
- Query the DB for documents with missing embeddings
- Embed those documents
- Continue to entity selection + catalyst generation as normal

This is the critical fix. `refresh` should work on **whatever is in the DB**, regardless of whether it came from file scanning or `ingest`.

### 2. `enzyme init` should not wipe DB-ingested entries

**Location:** `crates/enzyme-cli/src/commands/init.rs`, `run_init()`

Currently calls `reset_vault_database()` which drops all tables and recreates them. This wipes any `ingest`-ed data.

**Required behavior:** When the vault has DB-ingested documents (no files on disk), `init` should:
- NOT call `reset_vault_database()`
- Instead, treat the existing DB-ingested documents as the corpus
- Run embedding + entity selection + catalyst generation on those documents
- Effectively: `init` on an ingest-only vault = `refresh` with full rebuild

Alternatively, `init` could detect that there are ingested docs and refuse to reset, printing a message like "vault has ingested data — use `refresh` instead."

### 3. `enzyme refresh` should accept `--collection`

**Location:** `crates/enzyme-cli/src/commands/refresh.rs`

The SDK uses `--collection <id>` to resolve vault paths via `~/.enzyme-sdk/collections/<id>`. `ingest` and `init` both accept `--collection`, but `refresh` may not (needs verification). If it doesn't, add it for consistency.

### 4. `ENZYME_HOME` isolation for SDK dev environment

**Already supported** — `ENZYME_HOME` overrides `~/.enzyme` for config.toml, auth.json, and models.

The SDK should set `ENZYME_HOME` to a SDK-specific directory (e.g., `~/.enzyme-sdk-dev/.enzyme`) so that:
- The SDK's dev vaults don't pollute the user's main `~/.enzyme/config.toml`
- Auth and model paths are isolated
- Multiple SDK instances don't conflict

**SDK change (not CLI):** Set `ENZYME_HOME` env var before calling the CLI binary.

## SDK Lifecycle (Once CLI Gaps Are Fixed)

### `connect_user(user_id)` — called on OAuth or `serve(init_users=...)`

```
1. Call @hydrate functions → get list of entries
2. enzyme ingest --collection {app}--{user_id}    # writes to DB, extracts entities
3. enzyme refresh --collection {app}--{user_id}   # embeds + generates catalysts
4. User is now searchable via catalyze/petri
```

### `on_save(user_id, entry)` — called on each app write

```
1. enzyme ingest --collection {app}--{user_id} (single entry)
2. Do NOT refresh immediately
3. Debounce: after N saves or T seconds, trigger refresh
```

**Rationale for debounce:** Catalyst generation is the expensive step (LLM call per entity). If a user saves 10 recipes in quick succession, we should ingest all 10 immediately (DB writes are fast) but only regenerate catalysts once after the burst settles. The search quality from embeddings alone (without new catalysts) is acceptable for the brief window.

### `serve(init_users=[...])` — dev mode startup

```
1. For each user in init_users:
   a. connect_user(user_id)  # hydrate + ingest + refresh (full pipeline)
2. Start MCP server
3. On tool calls: use catalyze/petri against the indexed collections
4. On incremental on_save: ingest + debounced refresh
```

### Search fallback

If catalyze returns empty results (e.g., no catalysts generated yet), the SDK falls back to keyword search over the in-memory entry store. This ensures the dev experience is never broken — you always get results, they're just better when the full pipeline has run.

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ENZYME_HOME` | Isolate SDK config/auth/models from user's main enzyme | `~/.enzyme` |
| `ENZYME_API_KEY` | API key for enzyme.garden (for hosted publishing) | from `~/.enzyme/auth.json` |
| `OPENAI_API_KEY` + `OPENAI_BASE_URL` | Override LLM provider for catalyst generation | OpenRouter via enzyme server |

## File Locations (SDK Dev Environment)

```
~/.enzyme-sdk/collections/
  {app}--{user_id}/
    .enzyme/
      enzyme.db          # SQLite: docs, chunks, entities, embeddings, catalysts
      enzyme.log

~/.enzyme/                # (or $ENZYME_HOME)
  config.toml            # vault configs, entity selections
  auth.json              # enzyme.garden API key
  models/                # local embedding model (ese)
```

## Priority

1. **P0: `refresh` processes DB-ingested entries** — This is the critical blocker. Without it, `ingest` → `refresh` doesn't produce embeddings or catalysts.
2. **P1: `init` doesn't wipe ingested data** — Currently destructive; should either detect and skip, or delegate to refresh.
3. **P1: `refresh --collection`** — Consistency with ingest/init.
4. **P2: ENZYME_HOME isolation in SDK** — Nice to have for clean dev environments.
