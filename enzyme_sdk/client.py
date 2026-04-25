"""EnzymeClient — wraps the Enzyme CLI binary via subprocess."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


def _json_default(value: Any) -> Any:
    """Serialize SDK-native values into the JSON shape expected by the CLI."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class CatalyzeResult:
    """A document retrieved by conceptual search.

    Each result is a full document (not a fragment) that matched via the
    thematic index. The similarity score reflects how well the document's
    associated questions aligned with your query.
    """

    file_path: str
    content: str
    similarity: float

    @property
    def filename(self) -> str:
        return self.file_path.rsplit("/", 1)[-1] if "/" in self.file_path else self.file_path


@dataclass
class ContributingCatalyst:
    """A thematic question that routed results to your query.

    Catalysts are generated from the corpus content — they characterize
    what each entity's documents are about. When a catalyst scores high
    against your query, it pulls in its associated documents.

    The `entity` field tells you which entity (tag, folder, person) this
    question belongs to. The `text` is the question itself.
    """

    text: str
    entity: str
    relevance_score: float
    contribution_count: int
    presentation_guidance: list[str]


@dataclass
class CatalyzeResponse:
    """Full response from a conceptual search.

    Contains ranked documents and the thematic questions that drove the
    retrieval. Use `top_contributing_catalysts` to understand *why*
    specific documents were surfaced — this is the signal an agent uses
    to focus its generation.
    """

    query: str
    results: list[CatalyzeResult]
    top_contributing_catalysts: list[ContributingCatalyst]
    processing_time: float
    total_results: int
    search_strategy: str

    def render_to_prompt(self) -> str:
        """Render search results as a prompt-ready string for an LLM.

        Returns a structured text block that an agent can use to ground
        its response in the user's actual content and patterns.
        """
        lines = [f"## Enzyme search: \"{self.query}\"", ""]

        if self.top_contributing_catalysts:
            lines.append("### Routing signals (catalysts that matched)")
            for cat in self.top_contributing_catalysts:
                guidance = ""
                if cat.presentation_guidance:
                    guidance = f" — {'; '.join(cat.presentation_guidance)}"
                lines.append(
                    f"- **{cat.entity}**: {cat.text} "
                    f"(relevance: {cat.relevance_score:.2f}, "
                    f"routed {cat.contribution_count} results{guidance})"
                )
            lines.append("")

        if self.results:
            lines.append("### Matched documents")
            for i, r in enumerate(self.results, 1):
                lines.append(f"#### {i}. {r.file_path} (similarity: {r.similarity:.3f})")
                # Truncate very long content for prompt efficiency
                content = r.content
                if len(content) > 2000:
                    content = content[:2000] + "\n[...truncated]"
                lines.append(content)
                lines.append("")

        return "\n".join(lines)


@dataclass
class PetriEntity:
    """An entity tracked in the index with its thematic questions.

    Entities are tags, folders, or wikilinks that appear across documents.
    Each entity's catalysts are the questions Enzyme generated to characterize
    what that entity's documents are about — these are the retrieval paths
    that route future queries to the right content.
    """

    name: str
    entity_type: str
    frequency: int
    catalysts: list[dict]
    activity_trend: str
    recency_score: float
    days_since_last_seen: int


@dataclass
class PetriResponse:
    """The conceptual index overview — what Enzyme understands about the corpus.

    Use this to see which entities are tracked, what questions characterize
    each one, and how active they are. This is the structural understanding
    that powers search.
    """

    entities: list[PetriEntity]
    total_entities: int
    applied_targets: list[dict] | None = None

    def render_to_prompt(self) -> str:
        """Render the index overview as a prompt-ready system context.

        Gives the agent a structural understanding of the user's corpus:
        what topics are active, what questions characterize each area,
        and how the user's interests have been shifting.
        """
        lines = [
            "## Enzyme context — what this user's corpus reveals",
            "",
            f"Tracking {self.total_entities} entities across the corpus.",
            "",
        ]

        for entity in self.entities:
            trend = entity.activity_trend
            recency = f"recency: {entity.recency_score:.1f}"
            days = entity.days_since_last_seen
            freshness = f"last seen: {days}d ago" if days > 0 else "active today"

            lines.append(
                f"### {entity.entity_type}: {entity.name} "
                f"({entity.frequency} occurrences, {trend}, {recency}, {freshness})"
            )

            if entity.catalysts:
                for cat in entity.catalysts[:5]:
                    text = cat.get("text", "")
                    if text:
                        lines.append(f"  - {text}")

            lines.append("")

        return "\n".join(lines)


@dataclass
class VaultStatus:
    """Index health — document count, embedding coverage, model info."""

    vault_path: str
    documents: int
    embedded: str
    entities: int
    catalysts: int
    model: str
    api_key_configured: bool


class EnzymeError(Exception):
    """Raised when an enzyme CLI command fails."""

    pass


def _install_binary(install_dir: Path) -> None:
    """Download the enzyme binary for the current platform."""
    import platform
    import tarfile
    import tempfile
    import urllib.request

    system = platform.system()
    machine = platform.machine()

    targets = {
        ("Darwin", "arm64"): "macos-arm64",
        ("Linux", "x86_64"): "linux-x86_64",
        ("Linux", "aarch64"): "linux-arm64",
    }

    target = targets.get((system, machine))
    if not target:
        raise EnzymeError(f"Unsupported platform: {system}-{machine}")

    repo = "jshph/enzyme"
    url = f"https://github.com/{repo}/releases/latest"
    with urllib.request.urlopen(url) as resp:
        version = resp.url.rsplit("/", 1)[-1]

    tarball_url = f"https://github.com/{repo}/releases/download/{version}/enzyme-{target}.tar.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = Path(tmpdir) / "enzyme.tar.gz"
        urllib.request.urlretrieve(tarball_url, tarball_path)

        with tarfile.open(tarball_path, "r:gz") as tar:
            tar.extractall(tmpdir)

        install_dir.mkdir(parents=True, exist_ok=True)
        src = Path(tmpdir) / "enzyme"
        dst = install_dir / "enzyme"
        src.rename(dst)
        dst.chmod(0o755)


class EnzymeClient:
    """Wraps the Enzyme CLI binary for programmatic access.

    The client calls `enzyme` via subprocess. All state lives on disk
    in the vault's `.enzyme/` directory — no external services required.

    Example — search an existing vault:

        client = EnzymeClient()
        results = client.catalyze("how they think about constraints", vault="~/notes")
        for r in results.results:
            print(r.file_path, r.similarity)

    Example — see what the index understands:

        overview = client.petri(vault="~/notes", top=5)
        for entity in overview.entities:
            print(f"{entity.name}: {entity.catalysts[0]['text']}")

    Example — ensure binary is installed before using:

        client = EnzymeClient.ensure_installed()
    """

    def __init__(self, enzyme_bin: str = "enzyme", timeout: int = 300):
        self.enzyme_bin = enzyme_bin
        self.timeout = timeout

    @classmethod
    def ensure_installed(cls, *, install_dir: str | None = None, timeout: int = 300) -> "EnzymeClient":
        """Return a client, installing the binary + model if needed.

        Checks if `enzyme` is on PATH. If not, downloads the platform
        binary from GitHub releases and runs `enzyme setup` to fetch
        the embedding model. Idempotent — does nothing if already installed.

        Args:
            install_dir: Where to put the binary (default: ~/.local/bin).
            timeout: Command timeout in seconds.
        """
        import shutil

        install_path = Path(install_dir) if install_dir else Path.home() / ".local" / "bin"
        enzyme_path = install_path / "enzyme"

        if shutil.which("enzyme"):
            return cls(timeout=timeout)

        if enzyme_path.exists():
            return cls(enzyme_bin=str(enzyme_path), timeout=timeout)

        print("enzyme binary not found — installing...")
        _install_binary(install_path)

        print("Downloading embedding model...")
        subprocess.run(
            [str(enzyme_path), "setup"],
            check=True, capture_output=True, text=True, timeout=120,
        )

        print(f"Installed to {enzyme_path}")
        return cls(enzyme_bin=str(enzyme_path), timeout=timeout)

    def _run(
        self,
        args: list[str],
        vault: str | None = None,
        collection: str | None = None,
    ) -> subprocess.CompletedProcess:
        cmd = [self.enzyme_bin]
        if collection:
            cmd.extend(["--collection", collection])
        elif vault:
            cmd.extend(["--vault", str(vault)])
        cmd.extend(args)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout,
            )
        except FileNotFoundError:
            raise EnzymeError(
                f"Enzyme binary not found at '{self.enzyme_bin}'. "
                "Install via: brew install jshph/enzyme/enzyme-cli"
            )
        except subprocess.TimeoutExpired:
            raise EnzymeError(f"Enzyme command timed out after {self.timeout}s: {' '.join(cmd)}")

        if result.returncode != 0:
            raise EnzymeError(f"Enzyme command failed (exit {result.returncode}): {result.stderr.strip()}")

        return result

    def _run_json(
        self,
        args: list[str],
        vault: str | None = None,
        collection: str | None = None,
    ) -> dict:
        result = self._run(args, vault=vault, collection=collection)
        stdout = result.stdout.strip()
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise EnzymeError(f"Failed to parse enzyme output: {e}\nOutput: {stdout[:500]}")

    def embed_entries(
        self,
        entries: list[dict] | None = None,
        *,
        entry: dict | None = None,
    ) -> dict:
        """Embed structured entries without requiring a vault or collection."""
        if (entries is None) == (entry is None):
            raise ValueError("Must provide exactly one of 'entries' or 'entry'")

        payload = {"entries": entries} if entries is not None else {"entry": entry}
        cmd = [self.enzyme_bin, "embed-entries"]

        try:
            result = subprocess.run(
                cmd,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise EnzymeError(
                f"Enzyme binary not found at '{self.enzyme_bin}'. "
                "Install via: brew install jshph/enzyme/enzyme-cli"
            )
        except subprocess.TimeoutExpired:
            raise EnzymeError(f"Enzyme command timed out after {self.timeout}s: {' '.join(cmd)}")

        if result.returncode != 0:
            raise EnzymeError(f"Embed entries failed (exit {result.returncode}): {result.stderr.strip()}")

        stdout = result.stdout.strip()
        if not stdout:
            return {}
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise EnzymeError(f"Failed to parse enzyme output: {e}\nOutput: {stdout[:500]}")

    def build_entry_cluster_index(self, entries: list[dict | str], **kwargs):
        """Build a reusable automatic entry-cluster index."""
        from enzyme_sdk.body_clusters import build_entry_cluster_index

        return build_entry_cluster_index(entries, client=self, **kwargs)

    def cluster_entries(self, entries: list[dict | str], **kwargs):
        """Cluster entries and append flat readable auto-cluster tags."""
        target_field = kwargs.pop("target_field", "tags")
        index = self.build_entry_cluster_index(entries, **kwargs)
        return index.assign(
            entries,
            text=kwargs.get("text"),
            target_field=target_field,
        )

    def catalyze(
        self,
        query: str,
        vault: str | Path | None = None,
        *,
        collection: str | None = None,
        limit: int = 10,
        register: str = "explore",
    ) -> CatalyzeResponse:
        """Search a vault by concept.

        The query doesn't need to match any text in the documents.
        Enzyme matches it against precomputed thematic questions, then
        retrieves the documents those questions point to. A broad query
        like "how does this person think about craft" works because the
        questions already encode the specific patterns in the content.

        Args:
            query: What you're looking for, in natural language.
            vault: Path to the vault directory.
            limit: Max results to return.
            register: Framing style — "explore" (open-ended), "continuity"
                      (follow-up), or "reference" (precise lookup).
        """
        args = ["catalyze", query, "-n", str(limit), "--register", register]
        data = self._run_json(args, vault=str(vault) if vault else None, collection=collection)

        results = []
        for r in data.get("results", []):
            results.append(CatalyzeResult(
                file_path=r.get("file_path", ""),
                content=r.get("content", ""),
                similarity=r.get("similarity", 0.0),
            ))

        catalysts = []
        for c in data.get("top_contributing_catalysts", []):
            catalysts.append(ContributingCatalyst(
                text=c.get("text", ""),
                entity=c.get("entity", ""),
                relevance_score=c.get("relevance_score", 0.0),
                contribution_count=c.get("contribution_count", 0),
                presentation_guidance=c.get("presentation_guidance", []),
            ))

        return CatalyzeResponse(
            query=data.get("query", query),
            results=results,
            top_contributing_catalysts=catalysts,
            processing_time=data.get("processing_time", 0.0),
            total_results=data.get("total_results", 0),
            search_strategy=data.get("search_strategy", "catalyze"),
        )

    def petri(
        self,
        vault: str | Path | None = None,
        *,
        collection: str | None = None,
        top: int | None = None,
        query: str | None = None,
    ) -> PetriResponse:
        """See what the index understands about the corpus.

        Returns the entities Enzyme tracks (tags, folders, links) and the
        thematic questions it generated for each. These questions are the
        retrieval paths — they determine what gets surfaced for a given query.

        Use this to understand the conceptual structure before searching,
        or to verify that new content produced meaningful questions after
        a refresh.

        Args:
            vault: Path to the vault directory.
            top: Number of top entities to return (by activity).
            query: Optional — rank entities by relevance to this query.
        """
        args = ["petri"]
        if top is not None:
            args.extend(["-n", str(top)])
        if query is not None:
            args.extend(["--query", query])

        data = self._run_json(args, vault=str(vault) if vault else None, collection=collection)

        entities = []
        for e in data.get("entities", []):
            entities.append(PetriEntity(
                name=e.get("name", ""),
                entity_type=e.get("type", ""),
                frequency=e.get("frequency", 0),
                catalysts=e.get("catalysts", []),
                activity_trend=e.get("activity_trend", ""),
                recency_score=e.get("recency_score", 0.0),
                days_since_last_seen=e.get("days_since_last_seen", 0),
            ))

        return PetriResponse(
            entities=entities,
            total_entities=data.get("total_entities", 0),
            applied_targets=data.get("applied_targets"),
        )

    def status(self, vault: str | Path | None = None, *, collection: str | None = None) -> VaultStatus:
        """Check index health — document count, embedding coverage, model."""
        result = self._run(["status"], vault=str(vault) if vault else None, collection=collection)
        text = result.stdout

        def _extract(pattern: str, default: str = "") -> str:
            m = re.search(pattern, text)
            return m.group(1).strip() if m else default

        return VaultStatus(
            vault_path=_extract(r"Vault:\s+(.+)"),
            documents=int(_extract(r"Documents:\s+(\d+)", "0")),
            embedded=_extract(r"Embedded:\s+(.+)"),
            entities=int(_extract(r"Entities:\s+(\d+)", "0")),
            catalysts=int(_extract(r"Catalysts:\s+(\d+)", "0")),
            model=_extract(r"Model:\s+(.+)"),
            api_key_configured=_extract(r"API key:\s+(.+)").lower() == "configured",
        )

    def ingest(
        self,
        vault: str | Path | None = None,
        *,
        collection: str | None = None,
        entries: list[dict] | None = None,
        entry: dict | None = None,
    ) -> dict:
        """Ingest structured data directly into the enzyme DB.

        Bypasses the filesystem — no markdown files are written.
        Documents are chunked, hashed, and indexed directly.

        Args:
            vault: Path to the vault directory.
            entries: List of document dicts to ingest (batch).
            entry: Single document dict to ingest (streaming).

        Each document dict can have:
            - title (required): Document title
            - content: Primary content text
            - notes: User annotations/notes
            - source: Source URL or reference
            - tags: List of tags for entity extraction
            - links: List of wikilinks / related items
            - folder: Folder grouping (e.g., "saves")
            - created_at: ISO 8601 timestamp or epoch millis
            - id: Unique identifier (auto-generated from title if omitted)
            - metadata: Arbitrary metadata dict
        """
        payload: dict = {}
        if entries is not None:
            payload["entries"] = entries
        elif entry is not None:
            payload["entry"] = entry
        else:
            raise ValueError("Must provide either 'entries' or 'entry'")

        if not vault and not collection:
            raise ValueError("Must provide either 'vault' or 'collection'")

        payload_json = json.dumps(payload, default=_json_default)

        cmd = [self.enzyme_bin]
        if collection:
            cmd.extend(["--collection", collection])
        elif vault:
            cmd.extend(["--vault", str(vault)])
        cmd.extend(["ingest", "--quiet"])

        try:
            result = subprocess.run(
                cmd,
                input=payload_json,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError:
            raise EnzymeError(
                f"Enzyme binary not found at '{self.enzyme_bin}'. "
                "Install via: brew install jshph/enzyme/enzyme-cli"
            )

        if result.returncode != 0:
            raise EnzymeError(
                f"Ingest failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        stdout = result.stdout.strip()
        if stdout:
            return json.loads(stdout)
        return {"status": "ok"}

    def init(self, vault: str | Path | None = None, *, collection: str | None = None, quiet: bool = True) -> dict | None:
        """Initialize a vault — runs the full indexing pipeline once.

        This embeds all documents, extracts entities, and generates the
        thematic questions that power search. Takes a few minutes on
        first run depending on corpus size.
        """
        args = ["init"]
        if quiet:
            args.append("--quiet")
        result = self._run(args, vault=str(vault) if vault else None, collection=collection)
        stdout = result.stdout.strip()
        if quiet and stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return None

    def refresh(self, vault: str | Path | None = None, *, collection: str | None = None, quiet: bool = True, full: bool = False) -> dict | None:
        """Update the index after adding or modifying documents.

        Only re-processes changed content unless `full=True`. Fast for
        incremental additions — the index evolves as the corpus grows.
        """
        args = ["refresh"]
        if quiet:
            args.append("--quiet")
        if full:
            args.append("--full")
        result = self._run(args, vault=str(vault) if vault else None, collection=collection)
        stdout = result.stdout.strip()
        if quiet and stdout:
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        return None

    @staticmethod
    def tool_description(tool_name: str) -> str:
        """Get the canonical tool description for agent harness registration.

        Args:
            tool_name: "catalyze" or "petri"

        Returns:
            A description string suitable for function_tool registration.
        """
        descriptions = {
            "catalyze": (
                "Search the user's accumulated content by concept. The query doesn't need "
                "to match document text — Enzyme routes it through precomputed thematic "
                "questions that characterize patterns in the user's choices. Use this when "
                "the user's question could benefit from their personal history, preferences, "
                "or past decisions. Returns matched documents and the thematic signals that "
                "drove the retrieval."
            ),
            "petri": (
                "Get a structural overview of the user's corpus — which topics are active, "
                "what thematic questions characterize each area, and how interests have "
                "shifted recently. Use this at session start to understand the user's "
                "landscape before responding."
            ),
        }
        if tool_name not in descriptions:
            raise ValueError(f"Unknown tool: {tool_name}. Available: {list(descriptions)}")
        return descriptions[tool_name]
