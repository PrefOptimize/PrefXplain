---
name: prefxplain
description: Generate or update an interactive dependency graph of the current codebase with natural-language descriptions for each file, rendered as a self-contained HTML the user can share. Use this skill whenever the user wants to understand, map, review, present, or explain the architecture of a codebase -- onboarding onto a new repo, preparing a walkthrough for a manager or teammate, pitching the code to an investor or client, or finding the core files vs the orphans. Trigger on indirect phrasings too, "help me understand this repo", "what's the structure here", "I need to present the code to X", "what are the load-bearing files", "draw me a map of this project", "update the diagram", "refresh prefxplain", "show the graph", "open prefxplain".
license: Apache-2.0
allowed-tools: shell
---

# prefxplain

Produces `.prefxplain/<commit>/prefxplain.html` -- an interactive,
self-contained map of the codebase.
Nodes are files, edges are imports, and each node carries a 1-2 sentence
natural-language description written by you (the LLM running this skill).

**Smart re-runs**: if `.prefxplain/latest` points at a previous
`prefxplain.json`, descriptions, titles, flowcharts, groups, and highlights
from that run are preserved for files that still exist. Only new or
previously-undescribed files need work. This makes re-running cheap.
Exception: if the user passes a `$LEVEL` that differs from the prior run,
descriptions are re-generated in the new voice.

## Bootstrap

Run these checks at the very start of every invocation:

**1. Locate the `prefxplain` CLI.**

Try (in order) the PATH, the user-local shim, and the canonical clone venv —
the `./setup` installer drops the shim at `~/.local/bin/prefxplain` and the
venv at `~/.prefxplain/.venv/bin/prefxplain`:

```bash
PREFXPLAIN_BIN="$(command -v prefxplain 2>/dev/null \
  || ([ -x "$HOME/.local/bin/prefxplain" ] && echo "$HOME/.local/bin/prefxplain") \
  || ([ -x "$HOME/.prefxplain/.venv/bin/prefxplain" ] && echo "$HOME/.prefxplain/.venv/bin/prefxplain") \
  || echo "")"
[ -n "$PREFXPLAIN_BIN" ] && "$PREFXPLAIN_BIN" --version

# Resolve the Python that has prefxplain importable. The inline
# `python -c "from prefxplain... "` snippets later MUST use this interpreter.
PREFXPLAIN_PYTHON="$(python3 -c 'import prefxplain' 2>/dev/null && echo python3 \
  || ([ -x "$HOME/.prefxplain/.venv/bin/python" ] && echo "$HOME/.prefxplain/.venv/bin/python") \
  || ([ -n "$PREFXPLAIN_BIN" ] && grep -m1 -oE '/[^[:space:]]+/bin/prefxplain' "$PREFXPLAIN_BIN" 2>/dev/null | sed 's|/prefxplain$|/python|') \
  || echo python3)"
```

If empty/exit≠0, tell the user (the **git-clone install is canonical**):

> prefxplain isn't installed. Recommended one-liner:
>
>     git clone --single-branch --depth 1 https://github.com/PrefOptimize/PrefXplain.git ~/.prefxplain && cd ~/.prefxplain && ./setup
>
> This builds an isolated venv, registers `/prefxplain` for every detected AI
> tool, and auto-installs the IDE preview extension. Want me to run it? [y/N]

Wait for confirmation. If yes, run the one-liner. If they prefer pip, accept
`pipx install prefxplain && prefxplain setup` (the bundled preview extension
auto-installs there too).

Use **`$PREFXPLAIN_PYTHON`** (not `python`) for every `python -c "..."` block
in the workflow below.

**2. Check the IDE preview extension.**

```bash
_term="$(printf '%s' "${TERM_PROGRAM:-}" | tr '[:upper:]' '[:lower:]')"
IDE_CLI=""
for _cli in "$_term" code-insiders cursor windsurf antigravity trae void vscodium codium positron code; do
  [ -z "$_cli" ] && continue
  _path="$(command -v "$_cli" 2>/dev/null || true)"
  if [ -n "$_path" ]; then IDE_CLI="$_path"; break; fi
done
if [ -z "$IDE_CLI" ]; then
  for _app in \
    "/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code" \
    "/Applications/Cursor.app/Contents/Resources/app/bin/cursor" \
    "/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf" \
    "/Applications/Antigravity.app/Contents/Resources/app/bin/antigravity" \
    "/Applications/Trae.app/Contents/Resources/app/bin/trae"; do
    if [ -x "$_app" ]; then IDE_CLI="$_app"; break; fi
  done
fi
if [ -n "$IDE_CLI" ]; then
  "$IDE_CLI" --list-extensions 2>/dev/null | grep -q "prefxplain.prefxplain-vscode" && echo "EXTENSION_OK" || echo "EXTENSION_MISSING"
else
  echo "NO_IDE_CLI"
fi
```

If `EXTENSION_MISSING`, re-run `prefxplain setup` silently — its
`_install_vscode_extension` helper will find a `.vsix` (in the wheel or in
the clone's `prefxplain-vscode/`) and install it via `code --install-extension`:

```bash
"$PREFXPLAIN_BIN" setup 2>&1 | grep -i "preview extension" || true
```

If still missing afterwards (no `.vsix` reachable, or no Node to build one),
tell the user once: open the HTML path in a browser. If `NO_IDE_CLI`, skip
silently. Never start a localhost server unless the user asks.

## Division of labor

- **The package** (`prefxplain.analyzer`, `prefxplain.renderer`, `prefxplain.graph`)
  handles: filesystem walk, AST/regex parsing for Python, JS/TS, C/C++, Go, Rust,
  Java, and Kotlin, graph construction, metrics (in/out-degree, cycles), automatic
  layering by abstraction level, and HTML rendering. Exclusions are built into the
  walker: `node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `.git/`.

- **You** (the Copilot agent executing this skill) handle: reading each file and
  writing a good description. This is the only LLM-dependent step, and because
  you run in-session, no API key is needed -- which makes the command free for
  the user (covered by the Copilot subscription).
  Do **not** invoke `describer.py` (that is the API-based fallback path).

## Workflow

### 1. Resolve the repo root and audience level

Parse the user's prompt into two things: `$LEVEL` (how descriptions sound) and
`$REPO` (the directory to analyze).

**Level**: if the first token is one of `newbie`, `middle`, `strong`, or
`expert`, consume it as `$LEVEL`. Otherwise set `$LEVEL=""` — the preservation
step below will adopt the prior run's level, or fall back to `newbie` on a
first run.

**Repo**: the next token (if any) is the path. Otherwise use the current
working directory. Store as `$REPO`.

Before any later Bash block, canonicalize `$REPO` so artifact paths keep
working after snippets `cd` into `.prefxplain/<commit>`:

```bash
REPO="$(cd "$REPO" && pwd)"
```

What each level means (voice used in step 4c):
- **newbie** — first-year CS student. Zero jargon. Plain verbs, concrete
  analogies. Explain what an outsider sees happening. *(default when unset)*
- **middle** — working developer. Standard industry terms without
  explanation. Skim-friendly one-liners.
- **strong** — senior engineer. Name patterns (visitor, SCC) without
  explaining them. Call out non-obvious invariants and trade-offs.
- **expert** — domain specialist. Skip introductions. Lead with what is
  unusual or decision-carrying. Precise vocabulary, no padding.

### 2. Analyze and save JSON (preserving previous descriptions)

`LEVEL` is exported into the environment so the python script can compare it
against the prior run. If they match (or either side is empty) the prior
descriptions are preserved; otherwise descriptions are cleared so step 4c
re-writes them in the new voice.

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" LEVEL="$LEVEL" "$PREFXPLAIN_PYTHON" -c "
import os
from pathlib import Path
from prefxplain.analyzer import analyze
from prefxplain.graph import Graph

root = Path(os.environ['REPO_ROOT'])
artifact = Path('.')
graph = analyze(root, max_files=500)

requested_level = (os.environ.get('LEVEL') or '').strip().lower()
valid_levels = {'newbie', 'middle', 'strong', 'expert'}
if requested_level and requested_level not in valid_levels:
    requested_level = ''

# Preserve descriptions, titles, flowcharts, groups, and highlights from previous run
prev = artifact / 'prefxplain.json'
prior_level = ''
if prev.exists():
    old = Graph.load(prev)
    prior_level = (getattr(old.metadata, 'level', '') or '').strip().lower()
    level_changed = bool(requested_level and prior_level and requested_level != prior_level)
    effective_level = requested_level or prior_level or 'newbie'

    if level_changed:
        # Drop prior descriptions so step 4c re-writes them in the new voice.
        # Keep group assignments — architecture doesn't change with level.
        old_map = {n.id: n for n in old.nodes}
        for node in graph.nodes:
            old_node = old_map.get(node.id)
            if old_node and old_node.group: node.group = old_node.group
        if old.metadata.groups: graph.metadata.groups = old.metadata.groups
        print(f'LEVEL_CHANGED: {prior_level} -> {requested_level}')
    else:
        old_map = {n.id: n for n in old.nodes}
        for node in graph.nodes:
            old_node = old_map.get(node.id)
            if old_node:
                if old_node.description: node.description = old_node.description
                if old_node.short_title: node.short_title = old_node.short_title
                if old_node.flowchart: node.flowchart = old_node.flowchart
                if old_node.group: node.group = old_node.group
                if old_node.highlights: node.highlights = old_node.highlights
        # Preserve summary/health/groups/group_highlights if they exist
        if old.metadata.summary: graph.metadata.summary = old.metadata.summary
        if old.metadata.health_score: graph.metadata.health_score = old.metadata.health_score
        if old.metadata.health_notes: graph.metadata.health_notes = old.metadata.health_notes
        if old.metadata.groups: graph.metadata.groups = old.metadata.groups
        if old.metadata.group_highlights: graph.metadata.group_highlights = dict(old.metadata.group_highlights)
else:
    effective_level = requested_level or 'newbie'

graph.metadata.level = effective_level

graph.save(artifact / 'prefxplain.json')
described = sum(1 for n in graph.nodes if n.description)
print(f'FILES: {len(graph.nodes)}')
print(f'EDGES: {len(graph.edges)}')
print(f'LANGUAGES: {graph.metadata.languages}')
print(f'DESCRIBED: {described}/{len(graph.nodes)}')
print(f'TRUNCATED: {len(graph.nodes) >= 500}')
print(f'LEVEL: {effective_level}')
"
```

Read the output. **If TRUNCATED is True**, stop and ask:

> I found N files and hit the 500-file cap. Want me to scope to a subdirectory
> (e.g. `src/`) or proceed on the truncated set?

Do not silently drop files.

**If DESCRIBED equals FILES** and `--force` was NOT passed, skip to step 4e
(just refresh the summary/health and re-render — all descriptions are current).

### 3. If `--no-descriptions` was passed, skip to step 5.

### 4. Fill in groups and descriptions

#### 4a. Define architectural groups

Before describing individual files, look at the file list and define 2-5
**architectural groups** that reflect how the codebase is actually organized.
These become the top-level blocks in the diagram.

Rules:
- Groups should reflect logical architecture, NOT directory structure.
  BAD: "prefxplain/", "tests/" (that's just folders)
  GOOD: "Analysis Pipeline", "Visualization Engine", "CLI & Exports"
- Group names MUST be self-explanatory. A reader who has never seen the codebase
  should understand what kind of files are inside just from the group name alone.
  BAD: "Integration & Tooling" (tooling for what? integration with what?)
  BAD: "Utilities", "Helpers", "Core", "Miscellaneous" (says nothing)
  GOOD: "CLI & Exports" (clear: the command-line interface and format exporters)
  GOOD: "Code Analysis" (clear: the part that analyzes code)
  GOOD: "Graph Visualization" (clear: the part that draws the graph)
  If you can't name a group clearly, it probably contains files that belong in
  different groups — split it up.
- Each group needs a short description (1 sentence) that appears on hover.
- Test files go in a "Tests" group — it will be visually de-emphasized.
- Every file must belong to exactly one group.
- Think: if you were drawing this on a whiteboard for a new teammate, what
  would the 2-4 big boxes be? Would the labels make sense without explanation?

Patch the groups into the JSON:

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THESE IN — group name → one-sentence description
graph.metadata.groups = {
    # "Analysis Pipeline": "Scans source files, parses imports, and builds the dependency graph.",
    # "Visualization Engine": "Renders the interactive HTML diagram with layout, clustering, and flowcharts.",
    # "Tests": "Automated test coverage for all modules.",
}

# FILL THESE IN — assign each file to a group
file_groups = {
    # "src/analyzer.py": "Analysis Pipeline",
    # "src/renderer.py": "Visualization Engine",
    # "tests/test_analyzer.py": "Tests",
}

for node in graph.nodes:
    if node.id in file_groups:
        node.group = file_groups[node.id]

graph.save(Path("prefxplain.json"))
print(f"Defined {len(graph.metadata.groups)} groups, assigned {len(file_groups)} files")
PYEOF
```

IMPORTANT: You MUST fill in both dicts with real values. Every file must be
assigned to a group. Group names should be human-readable, 1-3 words.

#### 4b. List undescribed nodes

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" -c "
import json
g = json.loads(open('prefxplain.json').read())
for n in g['nodes']:
    if not n.get('description'):
        print(n['id'])
"
```

If the list is empty, skip to step 4e.

#### 4c. Read files and write descriptions in batches

Process 10-20 files per batch. For each file:

**How much to read:**
- **Short files (<80 lines)**: read the whole thing.
- **Barrel/index files** (`__init__.py`, `index.ts`, `index.js`): read the whole
  thing -- the re-exports are the content.
- **Default (80-500 lines)**: imports block + top-level definitions + module
  docstring. Usually the first 60-120 lines.
- **Huge files (>500 lines)**: first 150 lines, then grep for `^class `, `^def `,
  `^export `, `^function ` to get the shape.

**Voice for `$LEVEL`** — apply this tone to `short_title`, `description`, and
the flowchart `label` / `description` fields below:

- **newbie** (default): first-year CS student. Zero jargon, every term glossed. Concrete
  analogies. Everyday verbs ("picks", "asks", "saves").
- **middle**: working developer. Standard industry terms used
  without explanation. Skim-friendly one-liners.
- **strong**: senior engineer. Name the pattern (visitor, circuit breaker)
  without explaining. Call out non-obvious invariants and trade-offs.
- **expert**: domain specialist. Lead with what is unusual or
  decision-carrying. Precise vocabulary, no padding.

**For each file, generate FOUR things:**

1. **`short_title`** (1-3 words): The role of the file shown on the diagram card.
   Think of it as a label you'd write on a box in an architecture whiteboard.
   - GOOD: `Graph Engine`, `HTML Renderer`, `JWT Validator`, `CLI Entry`, `AST Parser`
   - GOOD for tests: `Graph Tests`, `CLI Tests`, `Auth Tests`
   - BAD: `graph.py`, `utils`, `helpers` (filename or too vague)

2. **`description`** (1-2 sentences): What the file exposes and who uses it.
   Start with an active verb. Be specific enough that a reader who knows the
   domain could guess the file's role without opening it.

   Hard rules:
   - Don't start with "This file", "Contains", "Module for", "Handles".
   - Don't repeat the filename.
   - Don't hedge ("some", "various", "utilities for"). If it's truly a grab-bag,
     name the 2-3 main things.
   - Present tense, active voice.

   Examples:
   - GOOD: `Validates JWT tokens; exposes verify(token) which returns the decoded payload or raises AuthError.`
   - GOOD: `Builds the dependency graph via AST walking; main entry point for analyze() and Graph.from_root().`
   - BAD: `This file handles authentication logic for the app.` (starts with "This file", vague)
   - BAD: `Utilities for processing graph data and various helpers.` (hedged, grab-bag)

   Test files: describe what behavior is covered, not the framework.
   `Covers edge cases in Graph.add_edge, including self-referential cycles and missing imports.`

3. **`highlights`** (list of 0-3 strings): CONCRETE, codebase-specific facts
   about this file. Proper nouns only — named integrations, third-party tools,
   model names, hyperparameters, cloud providers, protocols, file formats, exact
   versions, CLI tools. NOT adjectives or architectural platitudes.
   - GOOD: `["Claude Code integration", "Codex CLI support", "SQLite cache"]`
   - GOOD: `["PyTorch", "lr=1e-4", "AdamW optimizer"]`
   - GOOD: `["GCP Cloud Run", "PostgreSQL via asyncpg"]`
   - BAD: `["handles user commands", "well-structured", "entry point"]`
   - If nothing concrete stands out for this file, use `[]`. Empty is better
     than generic.

4. **`flowchart`** (dict): A flowchart showing the ACTUAL logic flow of the file.
   This is what appears when a user double-clicks a block in the diagram.
   It MUST reflect the real behavior of the code, NOT be generic.

   Structure: `{"nodes": [...], "edges": [...]}`
   - Each node: `{"id": "1", "label": "3-6 words", "type": "start|end|decision|step",
     "description": "1 concrete sentence — hover tooltip"}`
   - Each edge: `{"from": "1", "to": "2", "label": "condition or empty string"}`

   Rules:
   - Use 3-7 nodes. One "start" node, one "end" node.
   - Decision nodes MUST have 2+ outgoing edges with meaningful condition labels
     (e.g. "yes"/"no", "found"/"not found", "valid"/"invalid", "Python"/"JS/TS").
   - Labels should describe what happens, not name functions
     (GOOD: "Parse import statements", BAD: "_analyze_python()")
   - **description** is the most important field — it's what appears on hover.
     Write it like you're explaining to a smart friend who doesn't know this
     codebase. Short, plain language, no jargon. One sentence max.
     BAD: "Calls ast.parse() on the file content, then walks the module body
     to extract ImportFrom and FunctionDef nodes." (too technical)
     BAD: "Processes the input data." (too vague)
     GOOD: "Reads the Python file and picks out every function, class, and
     import it finds."

   Example for an auth middleware:
   ```json
   {"nodes": [
     {"id": "1", "label": "Receive HTTP request", "type": "start",
      "description": "Every incoming request passes through here before reaching your route handlers."},
     {"id": "2", "label": "Authorization header present?", "type": "decision",
      "description": "Looks for a 'Bearer xxx' token in the request headers. No token means no entry."},
     {"id": "3", "label": "Validate JWT token", "type": "step",
      "description": "Checks that the token hasn't expired and was actually signed by your server."},
     {"id": "4", "label": "Return 401 Unauthorized", "type": "step",
      "description": "Blocks the request with a 401 error — the user needs to log in again."},
     {"id": "5", "label": "Token valid?", "type": "decision",
      "description": "If the token is expired or tampered with, the request gets rejected."},
     {"id": "6", "label": "Attach user to request", "type": "step",
      "description": "Saves the user's identity on the request so your route handlers know who's calling."},
     {"id": "7", "label": "Pass to next handler", "type": "end",
      "description": "The request continues to your actual route handler with the user info attached."}
   ], "edges": [
     {"from": "1", "to": "2", "label": ""},
     {"from": "2", "to": "3", "label": "yes"},
     {"from": "2", "to": "4", "label": "no"},
     {"from": "3", "to": "5", "label": ""},
     {"from": "5", "to": "6", "label": "valid"},
     {"from": "5", "to": "4", "label": "expired"},
     {"from": "6", "to": "7", "label": ""},
     {"from": "4", "to": "7", "label": ""}
   ]}
   ```

#### 4d. Patch the JSON after each batch

After writing descriptions for a batch, run this script with the dict filled in:

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THIS IN -- one entry per file in this batch
# Format: "path/to/file.py": ("Short Title", "Full description.", [highlights], {flowchart_dict}),
files = {
    # "src/auth.py": ("JWT Validator", "Validates JWT tokens; exposes verify(token).", ["PyJWT", "HS256"], {"nodes": [...], "edges": [...]}),
}

for node in graph.nodes:
    if node.id in files:
        entry = files[node.id]
        node.short_title = entry[0]
        node.description = entry[1]
        if len(entry) > 2 and entry[2]:
            node.highlights = list(entry[2])
        if len(entry) > 3 and entry[3]:
            node.flowchart = entry[3]

graph.save(Path("prefxplain.json"))
print(f"Patched {len(files)} files")
PYEOF
```

IMPORTANT: You MUST fill in the `files` dict with real values before running.
Each value is a tuple of `("Short Title", "Full description.", [highlights], {flowchart})`.
The flowchart dict is required — it MUST reflect the actual logic of the file.
Highlights may be an empty list when nothing concrete stands out.
Do NOT leave the placeholder comment. Run once per batch. Save after each batch.

#### 4e. Completeness check

After all batches, verify nothing was missed:

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" -c "
import json
g = json.loads(open('prefxplain.json').read())
missing = [n['id'] for n in g['nodes'] if not n.get('description')]
print(f'MISSING: {len(missing)}')
for f in missing: print(f'  {f}')
"
```

If MISSING > 0, go back and describe them.

#### 4f. Generate group-level highlights

After file-level highlights are in place, synthesize up to 3 group-level
highlights per architectural group — concrete facts that span multiple files
within the group (e.g. "supports Claude Code + Codex + Copilot" from three
sibling integration files, not from any single one).

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THESE IN — group name → list of up to 3 concrete, cross-file facts
graph.metadata.group_highlights = {
    # "CLI & Integrations": ["supports Claude Code + Codex + Copilot", "Typer CLI", "MCP stdio server"],
    # "Code Analysis": ["supports 7 languages", "Claude Sonnet 4.6 default", "SQLite cache"],
}

graph.save(Path("prefxplain.json"))
print(f"Patched {len(graph.metadata.group_highlights)} group highlights")
PYEOF
```

Skip groups where nothing concrete spans the files. Empty list is fine.

#### 4g. Generate executive summary + health score

This is the most important step. The summary is what founders paste into decks
and what devs read to understand a project in 30 seconds.

First, collect the structural signals you need. Run:

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" -c "
import json
from collections import Counter
g = json.loads(open('prefxplain.json').read())
indeg = Counter()
outdeg = Counter()
for e in g['edges']:
    indeg[e['target']] += 1
    outdeg[e['source']] += 1
print('FILES:', len(g['nodes']))
print('EDGES:', len(g['edges']))
print('LANGUAGES:', g['metadata']['languages'])
# Top 3 most-imported
for f, c in indeg.most_common(3):
    desc = next((n.get('description','') for n in g['nodes'] if n['id']==f), '')
    print(f'HUB: {f} ({c} imports) -- {desc}')
# Entry points
for n in g['nodes']:
    if indeg[n['id']] == 0 and not n['id'].startswith('tests/'):
        print(f'ENTRY: {n[\"id\"]}')
# Orphans
orphans = [n['id'] for n in g['nodes'] if indeg[n['id']]==0 and outdeg[n['id']]==0]
print(f'ORPHANS: {len(orphans)}')
# Cycles
cycles = g.get('cycle_node_ids', [])
print(f'CYCLES: {len(cycles)} nodes in cycles')
# Test ratio
tests = sum(1 for n in g['nodes'] if n['id'].startswith('tests/'))
print(f'TEST_RATIO: {tests}/{len(g[\"nodes\"])}')
"
```

Now look at the README too if it exists. Read `$REPO/README.md` (first 30 lines).
This gives you the project's own description of itself.

**Then write the summary and health score.** This is NOT an aggregation of the
per-file descriptions. It answers a different question: "What is this project,
how is it built, and should I be worried about the architecture?"

Use these structural signals + the README + your understanding from reading the
files to write:

- **summary**: 3-5 sentences. What does the project do? What are the main
  architectural layers? What's the critical path (entry -> core)? Mention
  specific file names for the load-bearing modules. A non-technical person
  should understand the first sentence; a dev should find the next 2-3 useful.

- **health_score**: integer 1-10. Based on: cycles (>0 = -2), orphan ratio
  (>20% = -1), test coverage (no tests = -3, <50% ratio = -1), single points
  of failure (one file with >50% of imports = -1), overall modularity.

- **health_notes**: 1-2 sentences interpreting the score. Name the specific
  risks. "No circular dependencies. graph.py is a single point of failure
  (13 of 17 files depend on it). Test coverage is solid (9 test files for
  8 source files)."

Patch them into the JSON:

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THESE IN from your analysis above
graph.metadata.summary = "..."
graph.metadata.health_score = 8
graph.metadata.health_notes = "..."

graph.save(Path("prefxplain.json"))
print("Patched summary + health")
PYEOF
```

IMPORTANT: The summary must NOT be generic. It must name specific files, specific
numbers, and specific architectural decisions. If it reads like it could describe
any project, rewrite it.

### 5. Render the final HTML

```bash
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PREFXPLAIN_PYTHON" -c "
from pathlib import Path
from prefxplain.graph import Graph
from prefxplain.renderer import render

graph = Graph.load(Path('prefxplain.json'))
output = Path('${OUTPUT:-prefxplain.html}')
render(graph, output_path=output)
print(f'Written: {output}')
print(f'OPEN: {output.resolve()}')
"
```

If the user passed `--output path`, use that path instead of the default.

### 6. Preview in IDE

Open the generated HTML in the installed PrefXplain IDE preview:

```bash
ARTIFACT_VERSION="$(cat "$REPO/.prefxplain/latest" 2>/dev/null || git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
HTML_PATH="$REPO/.prefxplain/$ARTIFACT_VERSION/prefxplain.html"
# All VS Code family IDEs (Cursor, Windsurf, Antigravity, Trae, Void,
# VSCodium, Positron, …) follow Microsoft's convention: the URI scheme is
# literally the IDE name. So `TERM_PROGRAM` is usually the right scheme.
IDE_SCHEME="$(python3 -c "
import os
term = (os.environ.get('TERM_PROGRAM') or '').lower().strip()
generic = {'', 'apple_terminal', 'iterm.app', 'hyper', 'tmux', 'rxvt', 'xterm', 'xterm-256color', 'alacritty', 'kitty', 'ghostty', 'warp', 'wezterm'}
print('vscode' if term in generic else term)
")"
PREVIEW_URI="$(HTML_PATH="$HTML_PATH" IDE_SCHEME="$IDE_SCHEME" python3 -c "import os, urllib.parse; print(f\"{os.environ['IDE_SCHEME']}://prefxplain.prefxplain-vscode/preview?path={urllib.parse.quote(os.environ['HTML_PATH'])}\")")"

# Dispatch the vscode:// URI to the local IDE. Strategy per platform:
#   - macOS:         `open <URI>` → LaunchServices → VS Code → extension handler
#   - Linux desktop: `xdg-open <URI>` (needs DISPLAY) → VS Code registered handler
#   - Windows/WSL:   `start "" <URI>` or `wslview <URI>`
#   - Headless SSH:  no display — nothing auto-opens. The clickable URI printed
#                    just below is the guaranteed fallback (VS Code's integrated
#                    terminal auto-links `vscode://` so the user Cmd/Ctrl+clicks).
case "$(uname -s 2>/dev/null)" in
  Darwin*)
    command -v open >/dev/null 2>&1 && open "$PREVIEW_URI" 2>/dev/null || true
    ;;
  Linux*)
    if [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then
      xdg-open "$PREVIEW_URI" 2>/dev/null || true
    elif command -v wslview >/dev/null 2>&1; then
      wslview "$PREVIEW_URI" 2>/dev/null || true
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    command -v start >/dev/null 2>&1 && start "" "$PREVIEW_URI" 2>/dev/null || true
    ;;
esac
```

Then report the path. The OSC 8 hyperlink escape binds a **short clickable
label** to the full URI, so the link stays Cmd/Ctrl-clickable even on narrow
terminals where a raw URI would wrap across two lines (VS Code only detects
links that fit on one line). Plain URI is printed right after as a copy-paste
fallback and for terminals that don't honor OSC 8:

```bash
printf '\n\033]8;;%s\033\\%s\033]8;;\033\\\n' \
  "$PREVIEW_URI" "▶ Open preview in IDE (Cmd/Ctrl+click here)"
echo ""
echo "URI:          $PREVIEW_URI"
echo "HTML on disk: $HTML_PATH"
echo "Fallback:     Cmd+Shift+P → PrefXplain: Preview diagram"
```

Do not start a localhost server by default. The plugin webview is the primary preview path.

### 7. Report to the user

Keep this tight. Pull structural insights from the JSON:

- **File count + languages** (note if the cap was hit)
- **Top 3 most-imported files** -- the load-bearing abstractions. Name them with
  their one-line description.
- **Entry points** (in-degree 0, excluding tests) -- where to start reading
- **Orphans** (no imports in or out) -- if >3, give the count, offer to list
- **Cycles** if detected -- flag as architectural debt
- **Preview — MUST be rendered as a Markdown link**, not as a bare URL, because
  Claude Code / Codex / Copilot / Gemini chat panes make Markdown links
  Cmd/Ctrl-clickable regardless of line wrap, whereas a bare URI in a Bash
  tool result is just text (OSC 8 escape sequences get stripped by the chat
  UI). Format exactly like this, substituting `$PREVIEW_URI` and `$HTML_PATH`:

  ```
  Preview: [▶ Open in IDE]($PREVIEW_URI) ([fallback]: `prefxplain.html` at $HTML_PATH, or `Cmd+Shift+P → PrefXplain: Preview diagram`)
  ```

  Do NOT claim "opened in the IDE webview" — on headless Remote-SSH / devcontainer
  / Codespaces, nothing auto-opens; the user has to click the link.

Close with: "Happy to walk through any specific file or cluster."
Don't preempt -- wait for the user to ask.

## Notes

- The HTML is self-contained, works offline, safe to share with non-technical
  stakeholders
- `.prefxplain/<commit>/prefxplain.json` stays on disk so re-running
  `/prefxplain` only describes new or changed files — previous descriptions
  are preserved automatically
- The HTML renderer already surfaces entry points, core files, orphans, and cycles
  visually -- the text report is a summary for people reading along in chat
- The IDE extension preview is the default viewing path. Use a localhost server only
  if the user explicitly asks for browser preview.
