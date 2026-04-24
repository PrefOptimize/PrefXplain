"""LLM-powered file description generator with SQLite cache.

Generates natural-language descriptions for each file node AND its symbols
(functions, classes). Caches by (file_path, SHA-256 of file content).

Default model: claude-sonnet-4-6 (strong descriptions out of the box).
Requires ANTHROPIC_API_KEY or OPENAI_API_KEY (or --api-key flag).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse, urlunparse

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .graph import Graph, GroupSummary, Node

console = Console()

CACHE_DIR = Path.home() / ".prefxplain"
CACHE_DB = CACHE_DIR / "cache.db"

# Default: Claude Sonnet 4.6 — better descriptions than Haiku with acceptable latency/cost
DEFAULT_MODEL = "claude-sonnet-4-6"
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
DEFAULT_OLLAMA_HOST = "127.0.0.1"
DEFAULT_OLLAMA_PORT = 11434


def _parse_port(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65535 else None


def _build_ollama_base(host: str | None = None, port: int | None = None) -> str:
    """Build Ollama's OpenAI-compatible base URL."""
    raw_host = (host or os.environ.get("OLLAMA_HOST") or DEFAULT_OLLAMA_HOST).strip()
    if not raw_host:
        raw_host = DEFAULT_OLLAMA_HOST
    if not raw_host.startswith(("http://", "https://")):
        raw_host = f"http://{raw_host}"

    parsed = urlparse(raw_host)
    scheme = parsed.scheme or "http"
    hostname = parsed.hostname or DEFAULT_OLLAMA_HOST
    env_port = _parse_port(os.environ.get("OLLAMA_PORT"))
    resolved_port = port or env_port or parsed.port or DEFAULT_OLLAMA_PORT
    host_for_netloc = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    return urlunparse((scheme, f"{host_for_netloc}:{resolved_port}", "/v1", "", "", ""))


def _resolve_client_config(
    api_key: str | None,
    api_base: str | None,
    *,
    ollama: bool = False,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
) -> tuple[str | None, str | None]:
    """Resolve API key/base without letting Ollama env vars change defaults."""
    if ollama:
        resolved_key = api_key or os.environ.get("OLLAMA_API_KEY") or "ollama"
        resolved_base = api_base or _build_ollama_base(ollama_host, ollama_port)
        return resolved_key, resolved_base

    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")
    resolved_base = api_base
    if not resolved_base and (api_key or os.environ.get("ANTHROPIC_API_KEY")):
        resolved_base = ANTHROPIC_BASE
    return resolved_key, resolved_base


def _resolve_model(model: str | None) -> str:
    """Pick the model to use for a describe call.

    Priority: explicit argument > $ANTHROPIC_MODEL env var > DEFAULT_MODEL.
    This lets Claude Code sessions (which set ANTHROPIC_MODEL) flow their
    currently-selected model into the describer without extra config.
    """
    if model:
        return model
    env_model = os.environ.get("ANTHROPIC_MODEL")
    if env_model:
        return env_model
    return DEFAULT_MODEL

# Audience level controls the voice of LLM descriptions. Each level tunes
# jargon tolerance, depth, analogy use, and what to skip. "newbie" is the
# default — plain, zero-jargon voice that works for the widest audience.
VALID_LEVELS = frozenset({"newbie", "middle", "strong", "expert"})
_DEFAULT_LEVEL = "newbie"

_LEVEL_VOICES = {
    "newbie": (
        "Reader is a first-year CS student. Zero jargon — if a term must appear, "
        "gloss it in plain words (\"a cache is a box that remembers recent results\"). "
        "Reach for concrete analogies. Prefer everyday verbs (\"picks\", \"asks\", "
        "\"saves\") over technical ones (\"invokes\", \"resolves\", \"persists\"). "
        "Describe what an outsider observes happening, not how the code mechanically works."
    ),
    "middle": (
        "Reader is a working developer familiar with common patterns (REST, MVC, "
        "caching, ORMs, CI). Use standard industry terms without explaining them. "
        "Focus on what the code does and how it fits into the system. "
        "Skim-friendly one-sentence summaries."
    ),
    "strong": (
        "Reader is a senior engineer with deep system-design experience. Be concise "
        "and specific. Name the pattern (visitor, circuit breaker, SCC) — don't "
        "explain it. Call out non-obvious invariants, constraints, and trade-offs "
        "instead of restating the obvious."
    ),
    "expert": (
        "Reader is a domain specialist. Skip introductions. Lead with what is "
        "unusual or decision-carrying — which algorithm variant, which API boundary, "
        "which trade-off. Precise technical vocabulary. No padding."
    ),
}


def _normalize_level(level: str | None) -> str:
    """Coerce an arbitrary string to one of VALID_LEVELS; fall back to default."""
    if not level:
        return _DEFAULT_LEVEL
    candidate = level.strip().lower()
    return candidate if candidate in VALID_LEVELS else _DEFAULT_LEVEL


# v3 system prompt — extends v2 with semantic scaffolding (role/flow/extends_at/pattern)
# so a reader forms a mental model of the file in one glance instead of scrolling
# code. These fields feed the enriched detail panel; pithy bullets, not essays.
_SYSTEM_PROMPT_V2_TEMPLATE = """\
You write block descriptions for an interactive codebase architecture diagram.
Audience voice: __VOICE__
Given a source file, output JSON describing it.
Rules:
- "file": 1-2 sentences. Start with a verb (e.g. "Handles...", "Reads...", "Manages..."). Do not mention the filename.
- "symbols": for each symbol name, write 3-6 words saying what it does \
(e.g. "run": "starts the web server", "User": "stores user account data"). \
Only include functions and classes, not imports or variables.
- "highlights": aim for exactly 3 CONCRETE, codebase-specific facts that make this file \
interesting. Proper nouns: named integrations, third-party tools, model names, \
hyperparameters, cloud providers, protocols, file formats, CLI tools, exact thresholds. \
NOT adjectives or architectural platitudes. Keep each bullet under 40 chars — these \
render on the diagram card itself, so a reader should grok them at a glance. \
GOOD: ["Claude Code integration", "Codex CLI support", "SQLite cache"]. \
BAD: ["handles user commands", "entry point for CLI", "well-structured module"]. \
Return [] only if truly nothing concrete stands out. Empty is better than generic.
- "semantic_role": EXACTLY one of these keywords: "hub" (many files depend on this), \
"gateway" (external entry — CLI/HTTP/API boundary), "pipeline" (orchestrates a sequence \
of steps), "adapter" (wraps/translates an external service), "sink" (terminal — writes \
outputs, no consumers), "standalone" (isolated utility). Pick the single best fit, or \
"" if none apply.
- "flow": ONE short sentence, max ~15 words, of the form "receives X from Y, produces Z \
for W" or "reads X, emits Y on Z". Describe the DATA flow as an outsider would observe \
it. Skip if the file is pure configuration. Empty string "" is better than generic.
- "extends_at": ONE sentence naming the concrete extension point for someone adding a \
feature here (e.g. "register a new rule type in RULE_HANDLERS", "add a subclass of \
Exporter", "add a key to _LEVEL_VOICES"). Empty string "" if not extendable.
- "pattern": EXACTLY one keyword if obvious: "registry", "pipeline", "state-machine", \
"visitor", "factory", "strategy", "singleton", "decorator". "" otherwise. One word, \
lowercase.
- "flowchart": a flowchart showing the main logic of the file. \
Use 3-7 nodes. Each node has "id" (string "1","2"...), "label" (3-6 words describing the step), \
"type" ("start", "end", "decision", or "step"), and "description" (1 concrete sentence \
explaining what happens here). \
Each edge has "from" and "to" (node ids) and "label" (condition text for decisions, or "" for unconditional). \
The flowchart should reflect the ACTUAL logic flow, not be generic. \
Decision nodes MUST have at least 2 outgoing edges with meaningful condition labels (e.g. "yes"/"no", "found"/"not found", "valid"/"invalid"). \
Start with one "start" node and end with one "end" node.
Respond with valid JSON only, no markdown fences.
Example: {"file": "Manages database connections.", "symbols": {"connect": "opens a database connection"}, \
"highlights": ["PostgreSQL driver", "pgbouncer pooling", "SSL required"], \
"semantic_role": "adapter", \
"flow": "receives query requests from handlers, returns rows via the pool", \
"extends_at": "add a new dialect by implementing the Driver protocol", \
"pattern": "pool", \
"flowchart": {"nodes": [{"id": "1", "label": "Receive query request", "type": "start", \
"description": "Someone wants to run a database query."}, \
{"id": "2", "label": "Connection pool available?", "type": "decision", \
"description": "Checks if there's already an open connection we can reuse instead of creating a new one."}, \
{"id": "3", "label": "Reuse existing connection", "type": "step", \
"description": "Grabs an idle connection from the pool — faster than opening a new one."}, \
{"id": "4", "label": "Create new connection", "type": "step", \
"description": "Opens a fresh connection to the database since the pool was empty."}, \
{"id": "5", "label": "Return query results", "type": "end", \
"description": "Runs the SQL, sends back the rows, and returns the connection to the pool for reuse."}], \
"edges": [{"from": "1", "to": "2", "label": ""}, \
{"from": "2", "to": "3", "label": "available"}, \
{"from": "2", "to": "4", "label": "empty"}, \
{"from": "3", "to": "5", "label": ""}, \
{"from": "4", "to": "5", "label": ""}]}}
"""


def build_system_prompt_v2(level: str | None = None) -> str:
    """Return the v2 file-description system prompt tailored to the given audience level."""
    voice = _LEVEL_VOICES[_normalize_level(level)]
    return _SYSTEM_PROMPT_V2_TEMPLATE.replace("__VOICE__", voice)


# Backwards-compat alias — default-level prompt, still imported by tests.
SYSTEM_PROMPT_V2 = build_system_prompt_v2(_DEFAULT_LEVEL)

SYSTEM_PROMPT_DETAIL = """\
You are a senior software engineer writing thorough documentation.
Given a source file's path, its top-level symbols, and a few lines of content,
write a detailed paragraph (4-6 sentences) describing:
1. What this file does and its primary responsibility
2. Key functions/classes and what they provide
3. How it fits into the broader system architecture
4. Any notable patterns, dependencies, or design decisions
Be specific. Do not repeat the filename. Write in present tense.
"""

USER_PROMPT_V2 = """\
File: {file_path}
Language: {language}
Functions/classes: {symbols}

First {preview_lines} lines:
```
{preview}
```

Respond with JSON as described."""

USER_PROMPT_DETAIL = """\
File: {file_path}
Language: {language}
Symbols: {symbols}

First {preview_lines} lines:
```
{preview}
```

Describe what this file does in a detailed paragraph (4-6 sentences)."""


def _init_cache() -> sqlite3.Connection:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_DB))
    # v1: plain text descriptions (kept for backward compat)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS descriptions (
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY (file_path, content_hash)
        )
    """)
    # v2: JSON blob with file description + per-symbol descriptions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS descriptions_v2 (
            file_path TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (file_path, content_hash)
        )
    """)
    # group_highlights: cached synthesis per group keyed by content signature
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_highlights (
            group_label TEXT NOT NULL,
            signature TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (group_label, signature)
        )
    """)
    # group_summaries_v2: full semantic header (description + role + flow + extends_at
    # + highlights). Kept in its own table so the schema can evolve without
    # stomping the highlights-only v1 cache that older versions still populate.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_summaries_v2 (
            group_label TEXT NOT NULL,
            signature TEXT NOT NULL,
            data TEXT NOT NULL,
            PRIMARY KEY (group_label, signature)
        )
    """)
    conn.commit()
    return conn


def _content_hash(file_path: Path) -> str:
    try:
        return hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _get_cached(conn: sqlite3.Connection, file_path: str, content_hash: str) -> str | None:
    row = conn.execute(
        "SELECT description FROM descriptions WHERE file_path = ? AND content_hash = ?",
        (file_path, content_hash),
    ).fetchone()
    return row[0] if row else None


def _set_cached(conn: sqlite3.Connection, file_path: str, content_hash: str, description: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO descriptions (file_path, content_hash, description) VALUES (?, ?, ?)",
        (file_path, content_hash, description),
    )
    conn.commit()


def _file_preview(root: Path, node: Node, lines: int = 60) -> str:
    fpath = root / node.id
    try:
        if not fpath.resolve().is_relative_to(root.resolve()):
            return ""  # path traversal attempt
        text = fpath.read_text(encoding="utf-8", errors="ignore")
        return "\n".join(text.splitlines()[:lines])
    except OSError:
        return ""


def _make_prompt_v2(node: Node, root: Path) -> str:
    symbols = ", ".join(
        s.name for s in node.symbols[:20]
        if s.kind in ("function", "class")
    ) or "none"
    preview = _file_preview(root, node, lines=60)
    return USER_PROMPT_V2.format(
        file_path=node.id,
        language=node.language,
        symbols=symbols,
        preview=preview,
        preview_lines=60,
    )


def _make_prompt_detail(node: Node, root: Path) -> str:
    symbols = ", ".join(s.name for s in node.symbols[:20]) or "none"
    preview = _file_preview(root, node, lines=120)
    return USER_PROMPT_DETAIL.format(
        file_path=node.id,
        language=node.language,
        symbols=symbols,
        preview=preview,
        preview_lines=120,
    )


# Backward-compatible alias used by tests and older callers.
def _make_prompt(node: Node, root: Path) -> str:
    return _make_prompt_v2(node, root)


def _get_cached_v2(conn: sqlite3.Connection, file_path: str, content_hash: str) -> dict | None:
    row = conn.execute(
        "SELECT data FROM descriptions_v2 WHERE file_path = ? AND content_hash = ?",
        (file_path, content_hash),
    ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None


def _set_cached_v2(conn: sqlite3.Connection, file_path: str, content_hash: str, data: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO descriptions_v2 (file_path, content_hash, data) VALUES (?, ?, ?)",
        (file_path, content_hash, json.dumps(data, ensure_ascii=False)),
    )
    conn.commit()


_SYSTEM_PROMPT_GROUP_TEMPLATE = """\
You write the semantic header for a group of related files in an interactive \
codebase architecture diagram. The goal: a reader understands the group's role \
and data flow in 5 seconds without opening files.
Audience voice: __VOICE__
Given the group name and a list of its files (with short descriptions and any \
highlights extracted from each file), output JSON:
{"description": "...", "semantic_role": "...", "flow": "...", \
"extends_at": "...", "highlights": [...]}.
Rules:
- "description": ONE sentence (max ~20 words) starting with a verb describing what \
this group is responsible for AS A WHOLE. Not a catalog of files. Specific to THIS \
codebase, not a generic category label. GOOD: "Parses source files and validates \
architectural rules defined in .prefxplain.yml before a PR merges." \
BAD: "Contains analysis-related code." / "Exercises the behavior of the codebase."
- "semantic_role": EXACTLY one keyword: "hub" (core state other groups depend on), \
"gateway" (external boundary — CLI/HTTP/API), "pipeline" (orchestrates a sequence of \
stages), "adapter" (wraps/translates an external service), "sink" (terminal outputs), \
"standalone" (isolated capability). Empty "" if none applies.
- "flow": ONE short sentence of the form "receives X from Y, produces Z for W", \
describing how this group exchanges data with other groups. Skip if the group is a \
shared data model with no inbound flow to describe. Empty "" is better than generic.
- "extends_at": ONE sentence naming the extension point for someone adding a new \
capability INSIDE this group (e.g. "add a new rule class under rules/ and register \
it in RULE_HANDLERS"). Empty "" if not extendable.
- "highlights": up to 3 CONCRETE, group-wide facts that a user would find genuinely \
interesting about THIS group in THIS codebase. Proper nouns: integrations, supported \
tools, model names, protocols, file formats, exact versions. Empty [] is better than \
generic. Each highlight ≤40 chars — these render on the group card itself. \
GOOD: ["supports Claude Code + Codex", "SQLite-backed cache", \
"ANTHROPIC_API_KEY env var"]. BAD: ["handles CLI commands", "well-organized"].
- Respond with valid JSON only, no markdown fences.
"""


def build_system_prompt_group(level: str | None = None) -> str:
    """Return the group-highlight system prompt tailored to the given audience level."""
    voice = _LEVEL_VOICES[_normalize_level(level)]
    return _SYSTEM_PROMPT_GROUP_TEMPLATE.replace("__VOICE__", voice)


# Backwards-compat alias — default-level prompt, still imported by tests.
SYSTEM_PROMPT_GROUP = build_system_prompt_group(_DEFAULT_LEVEL)


def _group_signature(label: str, children: list[dict], level: str | None = None) -> str:
    """Build a content-hash signature for a group so we can cache its highlights.

    The level is included so cached highlights at one voice don't bleed into
    requests at another voice.
    """
    payload = json.dumps(
        {
            "label": label,
            "level": _normalize_level(level),
            "children": sorted((c.get("id", ""), c.get("hash", "")) for c in children),
        },
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _get_cached_group(
    conn: sqlite3.Connection, label: str, signature: str,
) -> list[str] | None:
    row = conn.execute(
        "SELECT data FROM group_highlights WHERE group_label = ? AND signature = ?",
        (label, signature),
    ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, list) else None


def _set_cached_group(
    conn: sqlite3.Connection, label: str, signature: str, highlights: list[str],
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO group_highlights (group_label, signature, data) VALUES (?, ?, ?)",
        (label, signature, json.dumps(highlights, ensure_ascii=False)),
    )
    conn.commit()


def _get_cached_group_summary(
    conn: sqlite3.Connection, label: str, signature: str,
) -> dict | None:
    row = conn.execute(
        "SELECT data FROM group_summaries_v2 WHERE group_label = ? AND signature = ?",
        (label, signature),
    ).fetchone()
    if not row:
        return None
    try:
        parsed = json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _set_cached_group_summary(
    conn: sqlite3.Connection, label: str, signature: str, summary: dict,
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO group_summaries_v2 (group_label, signature, data) VALUES (?, ?, ?)",
        (label, signature, json.dumps(summary, ensure_ascii=False)),
    )
    conn.commit()


def _parse_group_summary(raw: dict | None) -> tuple[GroupSummary, list[str]]:
    """Coerce an AI-generated group summary dict into (summary, highlights).

    Returns sanitized fields plus the highlights list so callers can keep
    storing highlights in the legacy `graph.metadata.group_highlights` map.
    """
    if not isinstance(raw, dict):
        return GroupSummary(), []
    summary = GroupSummary(
        description=_sanitize_short_sentence(raw.get("description"), max_len=240),
        semantic_role=_sanitize_enum(raw.get("semantic_role"), _VALID_SEMANTIC_ROLES),
        flow=_sanitize_short_sentence(raw.get("flow"), max_len=180),
        extends_at=_sanitize_short_sentence(raw.get("extends_at"), max_len=200),
        pattern=_sanitize_enum(raw.get("pattern"), _VALID_PATTERNS),
    )
    highlights = _clean_highlights(raw.get("highlights"))
    return summary, highlights


def describe_groups(
    graph: Graph,
    root: Path,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    force: bool = False,
    level: str | None = None,
    *,
    ollama: bool = False,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
) -> Graph:
    """Synthesize group-level highlights by reading child file descriptions.

    Groups with fewer than 2 member files are skipped — not enough signal.
    Results are written to graph.metadata.group_highlights and cached in sqlite
    keyed by group label + hash of child file contents + audience level.
    """
    model = _resolve_model(model)
    normalized_level = _normalize_level(level)
    system_prompt = build_system_prompt_group(normalized_level)
    try:
        from openai import OpenAI
    except ImportError:
        return graph

    graph.infer_groups()

    # Collect members per group label
    members_by_group: dict[str, list[Node]] = {}
    for node in graph.nodes:
        if not node.group:
            continue
        members_by_group.setdefault(node.group, []).append(node)

    eligible = {label: nodes for label, nodes in members_by_group.items() if len(nodes) >= 2}
    if not eligible:
        return graph

    resolved_key, resolved_base = _resolve_client_config(
        api_key=api_key,
        api_base=api_base,
        ollama=ollama,
        ollama_host=ollama_host,
        ollama_port=ollama_port,
    )

    client_kwargs: dict = {}
    if resolved_key:
        client_kwargs["api_key"] = resolved_key
    if resolved_base:
        client_kwargs["base_url"] = resolved_base

    try:
        client = OpenAI(**client_kwargs)
    except Exception as e:
        console.print(f"[yellow]Could not initialize LLM client for group highlights: {e}[/yellow]")
        return graph

    conn = _init_cache()
    try:
        if not getattr(graph.metadata, "group_highlights", None):
            graph.metadata.group_highlights = {}
        if not getattr(graph.metadata, "group_summaries", None):
            graph.metadata.group_summaries = {}
        if not getattr(graph.metadata, "groups", None):
            graph.metadata.groups = {}

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Synthesizing group summaries...", total=len(eligible))

            for label, nodes in sorted(eligible.items()):
                children_payload = []
                for n in nodes:
                    children_payload.append(
                        {
                            "id": n.id,
                            "hash": _content_hash(root / n.id),
                            "description": n.description,
                            "highlights": n.highlights,
                            "semantic_role": n.semantic_role,
                            "flow": n.flow,
                        }
                    )
                signature = _group_signature(label, children_payload, normalized_level)

                if not force:
                    cached_summary = _get_cached_group_summary(conn, label, signature)
                    if cached_summary is not None:
                        summary, highlights = _parse_group_summary(cached_summary)
                        _apply_group_summary(graph, label, summary, highlights)
                        progress.advance(task)
                        continue
                    # Legacy-cache fallback — highlights-only from an older run.
                    cached_highlights = _get_cached_group(conn, label, signature)
                    if cached_highlights is not None:
                        _apply_group_summary(
                            graph, label, GroupSummary(), cached_highlights,
                        )
                        progress.advance(task)
                        continue

                # Trim payload for prompt: top 12 files, description + highlights +
                # per-file semantic hints. The per-file role/flow help the model
                # synthesize a coherent group role without having to re-derive
                # structure from raw descriptions.
                trimmed = [
                    {
                        "file": c["id"],
                        "description": c["description"],
                        "highlights": c["highlights"],
                        "semantic_role": c.get("semantic_role", ""),
                        "flow": c.get("flow", ""),
                    }
                    for c in children_payload[:12]
                ]
                user_prompt = (
                    f'Group name: "{label}"\n'
                    f"Files in group: {len(nodes)}\n\n"
                    f"Children (JSON):\n{json.dumps(trimmed, ensure_ascii=False, indent=2)}\n\n"
                    "Respond with the group summary JSON as described."
                )

                summary = GroupSummary()
                highlights: list[str] = []
                try:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        max_tokens=500,
                        temperature=0.2,
                    )
                    content = response.choices[0].message.content if response.choices else None
                    raw = content.strip() if content else "{}"
                    if raw.startswith("```"):
                        raw = raw.split("```", 2)[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        raw = raw.strip()
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        parsed = None
                    summary, highlights = _parse_group_summary(parsed if isinstance(parsed, dict) else None)
                except Exception as e:
                    console.print(f"[yellow]  Warning: group summary call failed for {label}: {e}[/yellow]")

                _apply_group_summary(graph, label, summary, highlights)
                cache_payload = {**summary.to_dict(), "highlights": highlights}
                _set_cached_group_summary(conn, label, signature, cache_payload)
                # Also mirror highlights into the legacy cache so older
                # binaries reading group_highlights still pick them up.
                _set_cached_group(conn, label, signature, highlights)
                progress.advance(task)
    finally:
        conn.close()

    return graph


def _apply_group_summary(
    graph: Graph, label: str, summary: GroupSummary, highlights: list[str],
) -> None:
    """Write a synthesized group summary into all the places the renderer reads.

    - `group_summaries[label]` carries the full semantic scaffolding.
    - `groups[label]` mirrors the description so legacy renderers still see it,
      but ONLY when the LLM produced a real description (empty-string would wipe
      a helpful static fallback populated by `apply_inferred_groups`).
    - `group_highlights[label]` stays in sync with the new highlights list.
    """
    graph.metadata.group_summaries[label] = summary
    if summary.description:
        graph.metadata.groups[label] = summary.description
    graph.metadata.group_highlights[label] = list(highlights)


def describe(
    graph: Graph,
    root: Path,
    api_key: str | None = None,
    api_base: str | None = None,
    model: str | None = None,
    force: bool = False,
    detail: bool = False,
    progress_callback: Callable[[int, int], None] | None = None,
    level: str | None = None,
    *,
    ollama: bool = False,
    ollama_host: str | None = None,
    ollama_port: int | None = None,
) -> Graph:
    """Fill in descriptions for all nodes and their symbols.

    Uses SQLite cache — only calls the LLM for files that changed since last run.
    Defaults to claude-sonnet-4-6 via Anthropic API.

    Args:
        graph: Graph with nodes to describe.
        root: Repo root path (for reading file content).
        api_key: API key. Falls back to ANTHROPIC_API_KEY then OPENAI_API_KEY env vars.
        api_base: Optional API base URL. Defaults to Anthropic's endpoint.
        ollama: Use Ollama's OpenAI-compatible API endpoint.
        ollama_host: Ollama host/IP/domain. Only used when ollama is True.
        ollama_port: Ollama port. Only used when ollama is True.
        model: LLM model name. Default: claude-sonnet-4-6.
        force: Re-generate all descriptions, ignoring cache.
        detail: If True, generate paragraph-length file descriptions (no symbol descriptions).
        progress_callback: Called with (current, total) for custom progress reporting.
        level: Audience level for the voice of descriptions — one of newbie, middle,
            strong, expert. Unknown/empty falls back to newbie. Included in the cache
            key so changing level invalidates cached descriptions.

    Returns:
        The same graph with descriptions filled in on nodes and symbols.
    """
    try:
        from openai import OpenAI
    except ImportError:
        console.print("[yellow]openai package not installed. Run: pip install openai[/yellow]")
        console.print("[yellow]Skipping descriptions. Use --no-descriptions to suppress this warning.[/yellow]")
        return graph

    resolved_key, resolved_base = _resolve_client_config(
        api_key=api_key,
        api_base=api_base,
        ollama=ollama,
        ollama_host=ollama_host,
        ollama_port=ollama_port,
    )

    client_kwargs: dict = {}
    if resolved_key:
        client_kwargs["api_key"] = resolved_key
    if resolved_base:
        client_kwargs["base_url"] = resolved_base

    try:
        client = OpenAI(**client_kwargs)
    except Exception as e:
        console.print(f"[yellow]Could not initialize LLM client: {e}[/yellow]")
        console.print("[yellow]Skipping descriptions. Set ANTHROPIC_API_KEY or use --no-descriptions.[/yellow]")
        return graph

    resolved_model = _resolve_model(model)
    normalized_level = _normalize_level(level)
    conn = _init_cache()
    try:
        return _describe_with_conn(
            conn, graph, root, client, resolved_model, force, detail,
            progress_callback, normalized_level,
        )
    finally:
        conn.close()


def _validate_flowchart(fc: dict | None) -> dict | None:
    """Validate and sanitize an AI-generated flowchart structure."""
    if not fc or not isinstance(fc, dict):
        return None
    nodes = fc.get("nodes")
    edges = fc.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None
    if len(nodes) < 2 or len(nodes) > 12:
        return None
    valid_types = {"start", "end", "decision", "step", "entry", "process", "analysis", "data", "external", "test"}
    node_ids: set[str] = set()
    clean_nodes = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id", ""))
        label = str(n.get("label", ""))
        ntype = str(n.get("type", "step"))
        shape = str(n.get("shape", ntype or "step"))
        description = str(n.get("description", ""))
        if not nid or not label:
            continue
        if ntype not in valid_types:
            ntype = "step"
        if shape not in valid_types:
            shape = ntype
        node_ids.add(nid)
        clean_node = {"id": nid, "label": label, "type": ntype, "shape": shape}
        if description:
            clean_node["description"] = description
        # `insight` is the optional hover-tooltip field. Ship it through only
        # when the describer wrote something; empty strings become absent.
        insight = str(n.get("insight", "")).strip()
        if insight:
            clean_node["insight"] = insight
        clean_nodes.append(clean_node)
    clean_edges = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        efrom = str(e.get("from", ""))
        eto = str(e.get("to", ""))
        elabel = str(e.get("label", ""))
        if efrom in node_ids and eto in node_ids:
            clean_edges.append({"from": efrom, "to": eto, "label": elabel})
    if len(clean_nodes) < 2 or len(clean_edges) < 1:
        return None
    return {"nodes": clean_nodes, "edges": clean_edges}


MAX_HIGHLIGHTS = 3
HIGHLIGHT_MAX_LEN = 60

_GENERIC_HIGHLIGHT_TOKENS = (
    "handles ", "manages ", "provides ", "implements ", "contains ",
    "well-", "clean ", "modular ", "robust ", "comprehensive",
    "entry point", "main module", "core module", "utility module",
)


def _clean_highlights(raw: object) -> list[str]:
    """Validate and sanitize an AI-generated highlights list.

    Drops non-strings, strips, caps length, filters empty/generic entries,
    and truncates to MAX_HIGHLIGHTS.
    """
    if not isinstance(raw, list):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        text = item.strip().strip("-•*·").strip()
        if not text:
            continue
        if len(text) > HIGHLIGHT_MAX_LEN:
            text = text[: HIGHLIGHT_MAX_LEN - 1].rstrip() + "\u2026"
        lowered = text.lower()
        if any(tok in lowered for tok in _GENERIC_HIGHLIGHT_TOKENS):
            continue
        if lowered in seen:
            continue
        seen.add(lowered)
        cleaned.append(text)
        if len(cleaned) >= MAX_HIGHLIGHTS:
            break
    return cleaned


_VALID_SEMANTIC_ROLES = frozenset({
    "hub", "gateway", "pipeline", "adapter", "sink", "standalone",
})
_VALID_PATTERNS = frozenset({
    "registry", "pipeline", "state-machine", "visitor", "factory",
    "strategy", "singleton", "decorator", "pool", "observer",
})


def _sanitize_short_sentence(raw: object, max_len: int = 160) -> str:
    """Trim an AI-supplied short sentence: strip, bound length, drop fenced JSON."""
    if not isinstance(raw, str):
        return ""
    text = raw.strip().strip("`").strip()
    if not text:
        return ""
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "\u2026"
    return text


def _sanitize_enum(raw: object, allowed: frozenset[str]) -> str:
    if not isinstance(raw, str):
        return ""
    token = raw.strip().lower()
    return token if token in allowed else ""


def _apply_v2_data(node: Node, data: dict) -> None:
    """Apply a v2/v3 JSON response to a node.

    v3 adds semantic_role/flow/extends_at/pattern. Missing fields degrade
    gracefully to empty strings — the renderer just skips empty slots.
    """
    node.description = data.get("file", "").strip()
    sym_descs: dict[str, str] = data.get("symbols", {})
    for sym in node.symbols:
        if sym.name in sym_descs:
            sym.description = str(sym_descs[sym.name]).strip()
    flowchart = _validate_flowchart(data.get("flowchart"))
    if flowchart:
        node.flowchart = flowchart
    node.highlights = _clean_highlights(data.get("highlights"))
    node.semantic_role = _sanitize_enum(data.get("semantic_role"), _VALID_SEMANTIC_ROLES)
    node.flow = _sanitize_short_sentence(data.get("flow"), max_len=160)
    node.extends_at = _sanitize_short_sentence(data.get("extends_at"), max_len=180)
    node.pattern = _sanitize_enum(data.get("pattern"), _VALID_PATTERNS)


def _describe_with_conn(
    conn: sqlite3.Connection,
    graph: Graph,
    root: Path,
    client: object,
    model: str,
    force: bool,
    detail: bool,
    progress_callback: Callable[[int, int], None] | None,
    level: str = _DEFAULT_LEVEL,
) -> Graph:
    nodes_to_describe = [n for n in graph.nodes if not n.description or force]
    total = len(nodes_to_describe)

    if total == 0:
        return graph

    system_prompt_v2 = build_system_prompt_v2(level)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Generating descriptions...", total=total)

        for i, node in enumerate(nodes_to_describe):
            fpath = root / node.id
            content_hash = _content_hash(fpath)
            # Cache key carries the audience level so a voice change invalidates
            # cached descriptions — a re-run at a different level regenerates.
            if detail:
                cache_key = f"{node.id}:{level}:detail"
            else:
                cache_key = f"{node.id}:{level}"

            if not force and content_hash:
                if detail:
                    # detail mode: check v1 cache for plain text
                    cached = _get_cached(conn, cache_key, content_hash)
                    if cached:
                        node.description = cached
                        progress.advance(task)
                        if progress_callback:
                            progress_callback(i + 1, total)
                        continue
                else:
                    # normal mode: check v2 cache (JSON with symbol descriptions)
                    cached_data = _get_cached_v2(conn, cache_key, content_hash)
                    if cached_data:
                        _apply_v2_data(node, cached_data)
                        progress.advance(task)
                        if progress_callback:
                            progress_callback(i + 1, total)
                        continue
                    # Backward compatibility: accept legacy plain-text cache entries.
                    cached_text = _get_cached(conn, cache_key, content_hash)
                    if cached_text:
                        node.description = cached_text
                        progress.advance(task)
                        if progress_callback:
                            progress_callback(i + 1, total)
                        continue

            # Call LLM
            try:
                if detail:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": SYSTEM_PROMPT_DETAIL},
                            {"role": "user", "content": _make_prompt_detail(node, root)},
                        ],
                        max_tokens=400,
                        temperature=0.3,
                    )
                    content = response.choices[0].message.content if response.choices else None
                    node.description = content.strip() if content else ""
                    if content_hash and node.description:
                        _set_cached(conn, cache_key, content_hash, node.description)
                else:
                    response = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": system_prompt_v2},
                            {"role": "user", "content": _make_prompt_v2(node, root)},
                        ],
                        max_tokens=600,
                        temperature=0.2,
                    )
                    content = response.choices[0].message.content if response.choices else None
                    raw = content.strip() if content else "{}"
                    # Strip markdown fences if the model wrapped in ```json
                    if raw.startswith("```"):
                        raw = raw.split("```", 2)[1]
                        if raw.startswith("json"):
                            raw = raw[4:]
                        raw = raw.strip()
                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        # Fallback: treat as plain text file description
                        data = {"file": raw, "symbols": {}}
                    _apply_v2_data(node, data)
                    if content_hash and node.description:
                        _set_cached_v2(conn, cache_key, content_hash, data)
            except Exception as e:
                console.print(f"[yellow]  Warning: LLM failed for {node.id}: {e}[/yellow]")

            progress.advance(task)
            if progress_callback:
                progress_callback(i + 1, total)

    return graph
