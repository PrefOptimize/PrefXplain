---
name: prefxplain
description: Generate or update an interactive dependency graph of the current codebase with natural-language descriptions for each file, rendered as a self-contained HTML the user can share. Use this skill whenever the user wants to understand, map, review, present, or explain the architecture of a codebase -- onboarding onto a new repo, preparing a walkthrough for a manager or teammate, pitching the code to an investor or client, or finding the core files vs the orphans. Trigger on indirect phrasings too: "help me understand this repo", "what's the structure here", "I need to present the code to X", "what are the load-bearing files", "draw me a map of this project", "update the diagram", "refresh prefxplain", "show the graph", "open prefxplain".
argument-hint: [level: newbie|middle|strong|expert] [path] [--no-descriptions] [--output path/to/graph.html] [--include-changed] [--no-include-config]
allowed-tools: Bash, Read, Edit
---

# prefxplain

Produces `.prefxplain/<commit>/prefxplain.html` -- an interactive,
self-contained map of the codebase.
Nodes are files, edges are imports, and each node carries a 1-2 sentence
natural-language description written by you (the LLM running this skill).

**Smart re-runs**: if `.prefxplain/latest` points at a previous
`prefxplain.json` artifact, descriptions, titles, flowcharts, groups, and highlights
from that run are preserved for files that still exist. If the path you pass
points to a wrapper folder that sits one level above the actual project
(e.g. a monorepo root), step 1b will look up to 4 levels deep for a nested
PrefXplain artifact and auto-promote `$REPO` to that project, so prior
descriptions are reused instead of regenerated. Only new or
previously-undescribed files need work. This makes re-running cheap.
Exception: if `$LEVEL` changes between runs (e.g. the user ran
`/prefxplain newbie` yesterday and `/prefxplain expert` today),
descriptions are re-generated in the new voice.

## Bootstrap

Run these checks at the very start of every invocation:

**1. Check CLI availability:**

```bash
command -v prefxplain >/dev/null && prefxplain --version 2>/dev/null
```

If exit code != 0, tell the user:

> prefxplain is not installed on this machine. I can install it with:
> `curl -fsSL https://raw.githubusercontent.com/PrefOptimize/PrefXplain/main/install.sh | bash`
> OK? [y/N]

Wait for confirmation. If yes, run the curl|bash one-liner above.
Do NOT use `pip install prefxplain` — PyPI lags the GitHub main branch and
ships an older renderer. The installer clones fresh from main and sets up the
venv + shim + slash commands for you.

If the installer fails, stop and say what went wrong.

**1b. Resolve a Python interpreter that can import `prefxplain`.**

The installer guarantees `~/.prefxplain/.venv/bin/python` has prefxplain.
The user's shell may be inside a conda env, project venv, or virtualenv
where `prefxplain` is NOT installed — so `python -c "from prefxplain..."`
will fail with `ModuleNotFoundError`. Every Python block below MUST
resolve `$PPY` via the one-liner below (repeated in each block because
Bash tool calls do not share environment variables):

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
```

Then use `"$PPY"` in place of `python` or `python3`. If no interpreter
with prefxplain is found, stop and tell the user to re-run the installer.

**2. Check IDE extension (for in-IDE preview):**

Check both the CLI AND the on-disk extension directories — common macOS setups
ship VS Code/Cursor but don't add the `code`/`cursor` CLI to PATH, so a pure-
CLI check yields false negatives.

```bash
# Resolve PREFXPLAIN_ROOT: prefer the curl|bash install at ~/.prefxplain,
# fall back to the installed-package location if someone vendored it via
# `pip install -e` from a checkout.
if [ -d "$HOME/.prefxplain/prefxplain-vscode" ]; then
  PREFXPLAIN_ROOT="$HOME/.prefxplain"
else
  PREFXPLAIN_ROOT="$(prefxplain --help >/dev/null 2>&1 && python3 -c "import prefxplain, pathlib; print(pathlib.Path(prefxplain.__file__).parent.parent)" 2>/dev/null || echo "")"
fi
EXT_INSTALLED="no"
for ext_dir in "$HOME/.vscode/extensions" "$HOME/.cursor/extensions" "$HOME/.windsurf/extensions"; do
  if ls -d "$ext_dir"/prefxplain.prefxplain-vscode-* >/dev/null 2>&1; then
    EXT_INSTALLED="yes"
    break
  fi
done
# Fallback: CLI listing if on-disk check missed
if [ "$EXT_INSTALLED" = "no" ]; then
  IDE_CLI="$(which code 2>/dev/null || which cursor 2>/dev/null || which windsurf 2>/dev/null || echo "")"
  if [ -n "$IDE_CLI" ] && $IDE_CLI --list-extensions 2>/dev/null | grep -q "prefxplain.prefxplain-vscode"; then
    EXT_INSTALLED="yes"
  fi
fi
echo "EXT_INSTALLED: $EXT_INSTALLED"
```

If EXT_INSTALLED is "no" and `$PREFXPLAIN_ROOT` is set, install silently via
the Makefile target (it detects the IDE CLI and works even when none is on
PATH by falling back to the .app bundle):

```bash
[ -n "$PREFXPLAIN_ROOT" ] && cd "$PREFXPLAIN_ROOT" && make install-extension
```

If installation can't find any IDE, skip silently — preview will fall back
to the plain browser (step 6 prints both the `vscode://` deeplink and the
`http://` URL so the user can click whichever works).

## Division of labor

- **The package** (`prefxplain.analyzer`, `prefxplain.renderer`, `prefxplain.graph`)
  handles: filesystem walk, AST/regex parsing for Python, JS/TS, C/C++, Go, Rust,
  Java, and Kotlin, graph construction, metrics (in/out-degree, cycles), automatic
  layering by abstraction level, and HTML rendering. Exclusions are built into the
  walker: `node_modules/`, `.venv/`, `__pycache__/`, `dist/`, `build/`, `.git/`.

- **You** (the model executing this skill) handle: reading each file and writing a
  good description. This is the only LLM-dependent step, and because you run
  in-session, no API key is needed -- which makes the command free for the user.
  Do **not** invoke `describer.py` (that is the API-based fallback path).

## Model strategy

This skill runs in three cognitive modes. Use the right model for each:

- **Synthesis (steps 4a, 4f, 4g)** — architectural grouping, group-level
  highlights, and the executive summary + health score. Each of these
  reasons across the whole project at once and needs the strongest model
  available. **Keep the main orchestrating model** — prefer **Claude Opus
  4.7** on Claude Code / Cursor or **GPT-5.4** on Codex (whichever
  top-tier model is running this session). Do NOT delegate these steps.
- **Per-file work (step 4c)** — reading a single file and producing its
  `short_title`, `description`, `highlights`, `flowchart`, `invariants`,
  and `watch_if_changed`. This is the volume step (often 10-500+ files
  per run) and it is embarrassingly parallel. Run it on a small/fast
  model via parallel subagents whenever the runtime supports it —
  **Claude Haiku 4.5** on Claude Code, **`composer-2-fast`** (or
  **`claude-4.6-sonnet-medium-thinking`** if you want to stay on a
  Claude backbone) on Cursor, **GPT-5.4-mini** on Codex.
  Near-equivalent output quality at a fraction of the cost and
  wall-clock time.
- **Everything else** — bootstrap checks, analyzer calls, JSON patching,
  HTML rendering. Plain Bash + Python, no LLM work.

See step 4c for concrete dispatch instructions (including the Codex TOML
workaround) and the inline fallback for runtimes that can't select a
subagent model per call.

## Workflow

### 1. Resolve the repo root and audience level

Parse `$ARGUMENTS` into two things: `$LEVEL` (how the descriptions sound) and
`$REPO` (the directory to analyze).

**Level**: if the first token is one of `newbie`, `middle`, `strong`, or
`expert`, consume it as `$LEVEL`. Otherwise, set `$LEVEL=""` — the preservation
step below will adopt the prior run's level, or fall back to `newbie` on a
first run.

**Repo**: the next token (if any) is the path. Otherwise use the current
working directory. Store as `$REPO`.

Examples:
- `/prefxplain` → `$LEVEL=""`, `$REPO=.`
- `/prefxplain expert` → `$LEVEL=expert`, `$REPO=.`
- `/prefxplain newbie /path/to/repo` → `$LEVEL=newbie`, `$REPO=/path/to/repo`
- `/prefxplain /path/to/repo` → `$LEVEL=""`, `$REPO=/path/to/repo`
- `/prefxplain lewm-project` (with `lewm-project/le-wm/.prefxplain/latest`
  already existing) → parsing sets `$REPO=lewm-project`, then step 1b detects the
  nested analysis and promotes `$REPO=lewm-project/le-wm` so prior
  descriptions are preserved instead of regenerated

What each level means (use this for step 4c's voice):
- **newbie** — first-year CS student. Zero jargon. Plain verbs, concrete
  analogies. Explain what an outsider sees happening. *(default when unset)*
- **middle** — working developer. Standard industry terms without
  explanation. Skim-friendly one-liners.
- **strong** — senior engineer. Name patterns (visitor, SCC) without
  explaining them. Call out non-obvious invariants and trade-offs.
- **expert** — domain specialist. Skip introductions. Lead with what is
  unusual or decision-carrying. Precise vocabulary, no padding.

### 1b. Locate an existing PrefXplain artifact if $REPO doesn't contain one

If the user passes a path that is one level above the actual project folder
(e.g. a workspace with several projects, or they typed the parent by mistake),
the existing `.prefxplain/latest` marker won't be under `$REPO` and step 2
would miss the prior run. That is exactly what "smart re-runs" is meant to
avoid, so always reconcile `$REPO` with the nearest existing analysis before
touching the analyzer.

Before running the analyzer, verify whether `$REPO/.prefxplain/latest` exists
or a legacy `$REPO/prefxplain.json` exists. If not, do a shallow search
(depth 4, excluding heavy dirs):

```bash
if [ ! -f "$REPO/.prefxplain/latest" ] && [ ! -f "$REPO/prefxplain.json" ]; then
  find "$REPO" -maxdepth 4 -type f -name prefxplain.json \
    -not -path '*/node_modules/*' \
    -not -path '*/.git/*' \
    -not -path '*/.venv/*' \
    -not -path '*/__pycache__/*' \
    -not -path '*/dist/*' \
    -not -path '*/build/*' \
    2>/dev/null
fi
```

Interpret the output:
- **No matches** → leave `$REPO` unchanged. First-time analysis.
- **Exactly one match under `.prefxplain/<version>/prefxplain.json`** → set
  `$REPO` to the directory above `.prefxplain` and tell the user: *"Found
  existing PrefXplain artifacts at `<path>`. Treating that folder as the repo
  root to preserve prior descriptions."*
- **Exactly one legacy match at `<repo>/prefxplain.json`** → set `$REPO` to the
  parent of that `prefxplain.json` file and tell the user the same thing.
- **Multiple matches** → STOP. List the candidate directories and ask which
  one to update. Example:
  > I see multiple existing analyses under `$REPO`:
  > - `/workspace/lewm-project/le-wm/.prefxplain/abc123/prefxplain.json`
  > - `/workspace/lewm-project/web-ui/.prefxplain/def456/prefxplain.json`
  >
  > Which one should I update? (reply with the path, or "all fresh" to
  > re-analyze the parent from scratch)

Once `$REPO` is finalized, continue to step 2 with the adjusted value.
Before any later Bash block, canonicalize it once so artifact paths keep
working after snippets `cd` into `.prefxplain/<commit>`:

```bash
REPO="$(cd "$REPO" && pwd)"
```

### 2. Analyze and save JSON (preserving previous descriptions)

The `LEVEL` env var carries `$LEVEL` from step 1 into the python script. If it
matches the prior run's level (or if either side is empty), descriptions are
preserved. Otherwise, all descriptions/titles/flowcharts/highlights are cleared
so step 4c re-writes them in the new voice.

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" LEVEL="$LEVEL" "$PPY" -c "
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
        for node in graph.nodes:
            old_node = {n.id: n for n in old.nodes}.get(node.id)
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
                if old_node.highlights: node.highlights = list(old_node.highlights)
                if getattr(old_node, 'invariants', None):
                    node.invariants = list(old_node.invariants)
                if getattr(old_node, 'watch_if_changed', None):
                    node.watch_if_changed = list(old_node.watch_if_changed)
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

**Model:** Cross-file synthesis — keep the main orchestrating model
(Claude Opus 4.7 on Claude Code / Cursor, GPT-5.4 on Codex). Do not
delegate to a small/fast-model subagent.

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
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" << 'PYEOF'
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
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" -c "
import json
g = json.loads(open('prefxplain.json').read())
for n in g['nodes']:
    if not n.get('description'):
        print(n['id'])
" 
```

If the list is empty, skip to step 4e.

#### 4c. Read files and write descriptions in batches (Haiku 4.5 / GPT-5.4-mini)

**This step MUST run on parallel small/fast-model subagents.** Per the
"Model strategy" section, per-file work is the volume step (often
10-500+ files) and is embarrassingly parallel. Running it inline on the
main model (Opus 4.7 / GPT-5.4) is ~10× the cost and serial wall-clock,
so do not skip dispatch just because it feels simpler to loop locally.

**Algorithm (runtime-agnostic):**

1. Chunk the undescribed list from step 4b into batches of **10-20 file
   paths**. Ceil-divide: 37 files → 2 batches of ~19.
2. Dispatch **every batch concurrently** in a single turn (one assistant
   message with N parallel tool calls), using the runtime-specific
   recipe below. Do **not** dispatch sequentially — that gives you
   zero parallelism gain.
3. Each subagent returns one JSON dict keyed by file path. As results
   come back, feed each dict into step 4d and patch `prefxplain.json`
   immediately — per-batch writes mean a crashed subagent only loses
   its own batch.
4. The content rules (the `short`, `description`, `highlights`,
   `flowchart`, `invariants`, `watch` spec below this block) are the
   source of truth and MUST be passed inline to the worker in every
   dispatch call, delimited by `<CONTENT_RULES>` tags. Do not paraphrase
   them — copy verbatim.

---

**Claude Code dispatch (preferred runtime).** The `prefxplain-worker`
subagent is shipped with this skill (installed to
`~/.claude/agents/prefxplain-worker.md` by `prefxplain setup claude-code`)
and pins `model: haiku` in its frontmatter, so every Task call below
runs on Claude Haiku 4.5 — no `model` override needed.

For N batches, emit **one assistant turn** with N parallel Task calls.
Each call looks like:

```
Task(
  subagent_type="prefxplain-worker",
  description="prefxplain batch 1/N",
  prompt="""
$LEVEL: middle

Files in this batch:
- path/to/file_a.py
- path/to/file_b.ts
- ...

<CONTENT_RULES>
... copy the full six-field spec from the section below, verbatim ...
</CONTENT_RULES>

Return a single JSON dict keyed by the exact paths above, per the rules.
No prose, no markdown fences.
"""
)
```

If the `prefxplain-worker` subagent_type is not found, **STOP and tell
the user:**
> `prefxplain-worker` agent not found. Run `prefxplain setup` (or
> `prefxplain setup claude-code`) to install it, then re-run /prefxplain.

Do **not** fall through to the inline path — that runs Opus 4.7 at
~10× the cost and serial wall-clock. Do **not** substitute
`generalPurpose` — that defeats the cost split just as silently.

---

**Cursor dispatch.** The Task tool on Cursor does not expose
`haiku` as a model slug and the `prefxplain-worker` subagent_type
doesn't exist there — Cursor uses a fixed baked-in list (`explore`,
`generalPurpose`, ...). Use `subagent_type="explore"` (readonly is
fine, the worker only reads) with `model="composer-2-fast"` for the
small/fast path, or `model="claude-4.6-sonnet-medium-thinking"` if you
want to stay on a Claude backbone:

```
Task(
  subagent_type="explore",
  model="composer-2-fast",
  description="prefxplain batch 1/N",
  prompt="... same body as Claude Code, with $LEVEL, file list, and <CONTENT_RULES> ..."
)
```

Do not pass `model="haiku"` — it will be rejected. N parallel calls in
one turn, same as Claude Code.

---

**Codex dispatch.** Some Codex CLI versions silently ignore
`spawn_agent(model="gpt-5.4-mini")` and inherit the parent's GPT-5.4,
which defeats the cost split. The reliable path is the named agent
profile `prefxplain_worker` (installed to
`~/.codex/agents/prefxplain-worker.toml` by `prefxplain setup codex`)
with the model pinned in-file. Dispatch batches by name:

```
spawn_agent(
  "prefxplain_worker",
  prompt="... $LEVEL, file list, <CONTENT_RULES> ..."
)
```

…or `spawn_agents_on_csv` over the batched file list. The profile pins
`model = "gpt-5.4-mini"` so it can't silently fall back.

---

**Inline fallback — absolute last resort.** This path exists **only**
for runtimes with no subagent primitive whatsoever (e.g. a bare
Anthropic API harness). On Claude Code, Cursor, or Codex: **this path
MUST NOT run**. If the worker agent is missing, STOP and tell the user
to run `prefxplain setup`. Only proceed inline if (a) the runtime truly
has no Task/spawn_agent primitive, or (b) the user has seen the
missing-worker message and explicitly typed "continue inline anyway".
Even then, batch work (10-20 files per turn) and interleave step 4d
writes. A missing-worker message is a 10-second fix; running Opus
inline on 40 files costs ~$5 and several minutes of wall-clock.

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

- **newbie** (default): first-year CS student. Zero jargon. Every term glossed in plain
  words ("a cache is a box that remembers recent results"). Concrete analogies.
  Everyday verbs ("picks", "asks", "saves") instead of technical ones ("invokes",
  "resolves", "persists"). Describe what an outsider sees happening.
- **middle**: working developer familiar with REST/MVC/caching/ORM/CI.
  Standard industry terms used without explanation. Skim-friendly one-liners.
- **strong**: senior engineer. Name the pattern (visitor, circuit breaker, SCC)
  without explaining it. Call out non-obvious invariants, constraints, and
  trade-offs instead of restating the obvious.
- **expert**: domain specialist. Lead with what is unusual or decision-carrying
  (which algorithm variant, which API boundary, which trade-off). Precise
  technical vocabulary, no padding, no introductions.

**For each file, generate FOUR things:**

1. **`short_title`** (2-5 words, informative phrase): The file's role rendered
   as the **primary label** on the diagram card — the filename is demoted to
   a small mono subtitle underneath. Because the title owns the visual primacy,
   it must describe what the file DOES in words the audience (`$LEVEL`)
   actually recognises. Not a 1-word tag, not an abbreviation.

   Hard rules:
   - Do NOT use the filename or a slug of it (`graph.py` → BAD, `utils` → BAD).
   - Do NOT use bare abbreviations at newbie/middle level (`AR Predictor` →
     BAD; `Autoregressive Predictor` → GOOD).
   - Think "sticky note on a whiteboard": 2-5 words that land even without
     any bullets underneath.

   Examples:
   - GOOD: `Dependency Graph Builder`, `JWT Request Validator`, `HTML Diagram Renderer`, `Flowchart Popup UI`
   - GOOD for tests: `Graph Module Tests`, `CLI End-to-End Tests`
   - BAD: `Graph`, `Renderer`, `Utils`, `Core`, `graph.py`

2. **`description`** (1-2 sentences): What the file exposes and who uses it.
   Start with an active verb. Be specific enough that a reader who knows the
   domain could guess the file's role without opening it. Voice matches `$LEVEL`.

   Hard rules:
   - Don't start with "This file", "Contains", "Module for", "Handles".
   - Don't repeat the filename.
   - Don't hedge ("some", "various", "utilities for"). If it's truly a grab-bag,
     name the 2-3 main things.
   - Present tense, active voice.

   Examples (middle-level voice):
   - GOOD: `Validates JWT tokens; exposes verify(token) which returns the decoded payload or raises AuthError.`
   - GOOD: `Builds the dependency graph via AST walking; main entry point for analyze() and Graph.from_root().`
   - BAD: `This file handles authentication logic for the app.` (starts with "This file", vague)
   - BAD: `Utilities for processing graph data and various helpers.` (hedged, grab-bag)

   Test files: describe what behavior is covered, not the framework.
   `Covers edge cases in Graph.add_edge, including self-referential cycles and missing imports.`

3. **`highlights`** (list of **exactly 3 strings**): CONCRETE, codebase-specific
   facts about this file. These render as bullet points **on the diagram card
   itself**, so they are the second-chance summary: if the title + 3 bullets
   don't let a reader understand what the block does, the card has failed.

   Self-test before you save: *"If I only ever saw this card's title and 3
   bullets — no description, no code — would I understand this file's role in
   the project?"* If the answer is no, rewrite the bullets.

   Style:
   - Always exactly 3 bullets. No `[]`. No 1-bullet. No 2-bullet.
     If you're tempted to write fewer, you haven't looked hard enough: pick
     one behaviour, one input/output, and one dependency or constraint.
   - Each bullet **must be ≤48 characters** (fits the card without wrapping).
     Aim for 25-40 so the eye can scan.
   - **Self-explanatory at the target `$LEVEL`.** Spell out acronyms and
     domain jargon unless the audience would say them out loud. At newbie
     level: write `Autoregressive next-step prediction`, NOT `AR next-step
     pred`. At expert level `AR` is fine.
   - Mix across these three slots:
     1. a behaviour the file performs ("Caches descriptions in SQLite")
     2. a concrete input/output ("Emits prefxplain.json", "Reads *.py imports")
     3. a dependency, constraint, or named integration ("Uses Claude Code",
        "PyTorch 2.3", "Requires ANTHROPIC_API_KEY")
   - Fragments, not sentences — no trailing period. Leading capital OK.

   Examples (newbie voice):
   - GOOD: `["Picks out every import in a Python file", "Writes a JSON graph to disk", "Uses Python's built-in AST parser"]`
   - GOOD: `["Listens on port 8765", "Streams prefxplain.html changes live", "Talks to the VS Code preview tab"]`
   - BAD: `["AR next-step pred", "ViT encoder", "SIGReg"]`
     (abbreviations — a newbie cannot read this)
   - BAD: `["handles user commands", "well-structured", "entry point"]`
     (generic, no proper nouns or concrete facts)
   - BAD: `["uses a SQLite database for caching descriptions across runs"]`
     (too long, one instead of three)

4. **`flowchart`** (dict, **conditional — omit entirely if no real branches**):
   A flowchart that shows the ACTUAL branching logic of the file. The popup
   hides the flowchart panel unless the file genuinely has decisions —
   mapping a flat pipeline with `Start → Process → End` is pure noise
   ("premium noise"), so **only produce a flowchart when the file has at
   least one real conditional branch or state transition**.

   Decision rule: if you can't find at least one `if`/`match`/state-switch
   in the file whose condition names a concrete thing (env var, file type,
   numeric threshold, flag, error type), **leave `flowchart` as `None`**.
   The popup's File Brief already communicates Purpose / Consumes / Does /
   Produces without a diagram.

   Structure: `{"nodes": [...], "edges": [...]}`
   - Each node: `{"id": "1", "label": "3-6 words", "type": "start|end|decision|step",
     "description": "1 concrete sentence shown inside the block",
     "insight": "OPTIONAL — see rule below"}`
   - Each edge: `{"from": "1", "to": "2", "label": "condition or '' if no real transition name"}`

   **`insight` (optional, shown on hover).** A single non-obvious sentence
   that adds information the inline `description` cannot. The popup shows
   no tooltip unless this field has content — so *only write one when you
   genuinely have something new to say*. Good insights fall into these
   buckets:
   - **Failure mode**: "If the cache file is corrupt, we fall back to
     re-describing every file from scratch."
   - **Hidden contract / assumption**: "Assumes `$LEVEL` is already
     validated by the CLI — invalid values here become silent no-ops."
   - **Design rationale**: "Preserves prior descriptions so a flaky LLM
     call doesn't silently wipe good data."
   - **Performance note**: "Runs per file in a hot loop — every added
     regex doubles the cold-start time."

   Do **not** write an `insight` that:
   - Restates the description in different words.
   - Says the block is "important" or "central".
   - Documents what any reader can deduce from the label alone.

   If nothing non-obvious to add, set `"insight": ""` or omit the field.
   An empty insight is strictly better than filler.

   **Self-test before you save:** *"If I only saw the flowchart labels and
   descriptions — no code, no file header, nothing else — would I understand
   what this file does and what it really decides between? Could I roughly
   re-implement the file's behavior from this alone?"* If the answer is no,
   rewrite the flowchart.

   Hard rules:
   - 3-7 nodes. Exactly one "start", exactly one "end".
   - **Concrete identifiers**: at least 2 of the 3-7 nodes MUST name a real
     artifact from this file — a file path it reads or writes, an env var
     it checks, a port it binds, a CLI flag it parses, a DB table, an API
     endpoint, a file format it emits, a class/function name it defines.
     A flowchart without any proper nouns describes a template, not this
     file.
   - **Real decision conditions**: ban `"valid?"`, `"ok?"`, `"success?"`,
     `"found?"` in isolation — spell out the actual branching condition
     (`"path ends in .py?"`, `"$LEVEL unchanged from prior run?"`,
     `"files > 500 cap?"`, `"ANTHROPIC_API_KEY set?"`).
   - Decision nodes MUST have ≥2 outgoing edges with concrete condition
     labels (the real branching condition, not `"valid?"` / `"ok?"`).
   - Non-decision edges carry a label ONLY when the transition has a
     real name worth showing (`"on success"`, `"retry"`, `"cache miss"`,
     `"fallback"`). The generic "and then the next step runs" case uses
     `"label": ""` — the popup renders no chip at all. A chip that just
     says `then` is filler; omit it.
   - Labels describe what happens, not function names
     (GOOD: `"Parse import statements"`, BAD: `"_analyze_python()"`).
   - **description** is the most important field — it's what appears on
     hover. Write it at the same `$LEVEL` voice as the file description.
     One sentence, concrete, no jargon the audience can't parse.

   Failure modes to reject:
   - BAD (flat chain, no decisions, no proper nouns):
     ```
     Start → Initialize → Load data → Validate → Process → Return result → End
     ```
     This is the most common failure in the wild. If your flowchart looks
     like this, it describes any file in any project. Rewrite.
   - BAD (generic decisions):
     ```
     Start → Is input valid? → [yes: process / no: error] → End
     ```
     `"input valid?"` tells the reader nothing about this file.
   - GOOD (branch-and-merge with concrete identifiers):
     ```
     Start
      → Load prefxplain.json from the artifact directory
       → JSON exists AND $LEVEL matches prior run?
         ├─ yes → Preserve prior descriptions and flowcharts
         └─ no  → Clear descriptions so step 4c re-writes them
       → Save graph.metadata.level = effective_level
       → End
     ```

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

5. **`invariants`** (list of **exactly 2-3 short fragments**, ≤12 words each):
   Non-obvious rules a reader MUST keep true when editing this file. These
   appear in the popup's File Brief as "don't break" bullets — the founder's
   answer to *"what will bite me if I change this?"*.

   **Self-test:** *"Could a mid-level developer who hasn't seen this file
   before accidentally violate this invariant by making a plausible edit?"*
   If no (the invariant is obvious or enforced by the type system), **do not
   list it**. An empty list is better than filler.

   Hard rules:
   - Each fragment names one rule in plain active voice, no trailing period.
   - Must reference a **concrete artifact** from the file — a specific
     argument name, env var, format, type, or contract. No abstractions.
   - Skip the field entirely (`[]`) for trivial files (tests, simple data
     classes, single-responsibility utilities with no hidden contract).

   Examples:
   - GOOD for a path resolver: `["Absolute paths only — no relative paths leak out", "Cache key includes the $LEVEL env var", "Walks lazily, never materializes the full tree"]`
   - GOOD for a migration script: `["Idempotent — re-running must not double-apply", "Must run before 002_user_schema"]`
   - BAD: `["Uses good error handling", "Well-structured"]` (generic, no
     artifact)
   - BAD: `["Returns None on failure"]` (enforced by the signature; too
     obvious)

6. **`watch_if_changed`** (list of **exactly 2-3 short fragments**, ≤14 words
   each): Concrete blast-radius hints. The popup renders these as
   *"If you change this, watch…"* bullets — the founder's second-brain for
   impact analysis.

   Each fragment must name **a specific caller, a test, a UI surface, or an
   observable behavior** that would break first if this file's contract
   changed. This is not a reachability list (the graph already shows
   importers) — it's human-readable "first things to look at".

   Hard rules:
   - Name real artifacts: file names, test names, UI components, CLI flags.
   - Start with the thing to watch, not "check if" filler.
   - 2-3 items. If the file has no downstream effects (pure util, tests,
     orphan), use `[]`.

   Examples:
   - GOOD for a renderer: `["prefxplain.html layout", "VS Code preview extension's CSS contract", "README embedded screenshots"]`
   - GOOD for an analyzer: `["tests/test_analyzer.py — import resolution cases", "CLI `prefxplain check` exit codes", "cache invalidation across re-runs"]`
   - BAD: `["downstream code"]` (no artifact)
   - BAD: `["anything that uses this function"]` (that's just the import graph)

#### 4d. Patch the JSON after each batch

After writing descriptions for a batch, run this script with the dict filled in:

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THIS IN — one entry per file in this batch.
# Format: "path/to/file.py": {
#     "short": "Short Title",
#     "description": "Full description.",
#     "highlights": ["...", "...", "..."],             # exactly 3
#     "flowchart": {"nodes": [...], "edges": [...]},   # OR None if no real branches
#     "invariants": ["...", "..."],                     # 2-3, or [] when trivial
#     "watch": ["...", "..."],                          # 2-3, or [] when orphan
# }
files = {
    # "src/auth.py": {
    #     "short": "JWT Validator",
    #     "description": "Validates JWT tokens; exposes verify(token).",
    #     "highlights": ["PyJWT HS256", "15 min TTL", "Reads JWT_SECRET env"],
    #     "flowchart": {"nodes": [...], "edges": [...]},
    #     "invariants": ["Tokens are verified before any DB lookup", "JWT_SECRET must be 256 bits"],
    #     "watch": ["tests/test_auth.py", "middleware/session.py", "401 response format"],
    # },
}

for node in graph.nodes:
    entry = files.get(node.id)
    if not entry:
        continue
    node.short_title = entry["short"]
    node.description = entry["description"]
    node.highlights = list(entry.get("highlights") or [])
    flowchart = entry.get("flowchart")
    node.flowchart = flowchart if flowchart else None
    node.invariants = list(entry.get("invariants") or [])
    node.watch_if_changed = list(entry.get("watch") or [])

graph.save(Path("prefxplain.json"))
print(f"Patched {len(files)} files")
PYEOF
```

IMPORTANT:
- Fill in the `files` dict with real values before running — remove the
  placeholder comment.
- Each entry MUST provide `short`, `description`, and 3 `highlights`.
- `flowchart` is **conditional**: set it to `None` or omit it unless the
  file has real decision branches (step 4c rule 4).
- `invariants` and `watch` are 2-3 items each, or `[]` when genuinely
  trivial or orphan. No filler — see step 4c rules 5 & 6 for the self-tests.
- Run once per batch. Save after each batch.

#### 4e. Completeness check

After all batches, verify nothing was missed:

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" -c "
import json
g = json.loads(open('prefxplain.json').read())
missing = [n['id'] for n in g['nodes'] if not n.get('description')]
print(f'MISSING: {len(missing)}')
for f in missing: print(f'  {f}')
"
```

If MISSING > 0, go back and describe them.

#### 4f. Generate group-level highlights

**Model:** Cross-file synthesis — keep the main orchestrating model
(Claude Opus 4.7 on Claude Code / Cursor, GPT-5.4 on Codex). Do not
delegate.

After file-level highlights are in place, synthesize up to 3 group-level
bullets per architectural group — concrete facts that span multiple files
within the group (e.g. "supports Claude Code + Codex + Copilot" from three
sibling integration files, not from any single one). These render on the
group's card in the diagram.

Style (same constraints as file highlights):
- Each bullet ≤35 characters, fragment not sentence, proper nouns only.
- Voice matches `$LEVEL`.
- Empty list `[]` when nothing cross-file stands out. Don't pad.

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph

graph = Graph.load(Path("prefxplain.json"))

# FILL THESE IN — group name → list of up to 3 concrete, cross-file facts
graph.metadata.group_highlights = {
    # "CLI & Integrations": ["Claude Code + Codex + Copilot", "Typer CLI", "MCP stdio server"],
    # "Code Analysis": ["7 languages", "Claude Sonnet 4.6", "SQLite cache"],
}

graph.save(Path("prefxplain.json"))
print(f"Patched {len(graph.metadata.group_highlights)} group highlights")
PYEOF
```

Skip groups where nothing concrete spans the files. Empty list is fine.

#### 4f-bis. Categorize standalone files per group

**Model:** Main orchestrating model (same as 4f — cross-file synthesis).

For each group that has files in its standalone band (files with zero import
edges — detected in step 4b's output as files belonging to a group but not
appearing in any import), define 2-5 LLM-authored sub-categories. These render
as labeled sub-sections inside the standalone band instead of an unlabeled grid.

First, find which groups have standalone files:

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" -c "
import json
g = json.loads(open('prefxplain.json').read())
sources = {e['source'] for e in g['edges']}
targets = {e['target'] for e in g['edges']}
connected = sources | targets
by_group = {}
for n in g['nodes']:
    grp = n.get('group') or 'Ungrouped'
    if n['id'] not in connected:
        by_group.setdefault(grp, []).append(n['id'])
for grp, files in sorted(by_group.items()):
    print(f'GROUP: {grp} ({len(files)} standalone)')
    for f in files:
        print(f'  {f}')
"
```

For each group with standalone files, look at the file names and infer semantic
categories. Good categories describe **why** the files have no edges:
- `"CLI entrypoints"` — scripts invoked directly, not imported
- `"Packaging & config"` — setup.py, pyproject.toml, Dockerfile; not imported
- `"Static assets"` — CSS, SVG, images; not parsed by the import analyzer
- `"Test fixtures"` — data files loaded at runtime, not via import
- `"Vendor bundles"` — minified third-party JS that the bundler serves directly

Each category needs:
- `category`: short name (2-4 words)
- `description`: one sentence — what these files do AND why they have no edges
- `member_file_ids`: exact file IDs from the group's standalone list above

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" << 'PYEOF'
from pathlib import Path
from prefxplain.graph import Graph, GroupSummary, StandaloneCategory

graph = Graph.load(Path("prefxplain.json"))

# FILL THESE IN — one entry per group that has standalone files.
# Leave out groups with no standalone files.
taxonomy_by_group = {
    # "Diagram Frontend": [
    #     {
    #         "category": "Vendor bundles",
    #         "description": "Minified third-party JS served directly by the bundler — not parsed for imports.",
    #         "member_file_ids": ["elk-worker.min.js", "elk.bundled.js"],
    #     },
    #     {
    #         "category": "UI Components",
    #         "description": "Self-contained SVG/JSX panels imported via dynamic bundler paths the analyzer doesn't trace.",
    #         "member_file_ids": ["card-file.js", "group-hero.js"],
    #     },
    # ],
}

for group_name, categories in taxonomy_by_group.items():
    if group_name not in graph.metadata.group_summaries:
        graph.metadata.group_summaries[group_name] = GroupSummary()
    summary = graph.metadata.group_summaries[group_name]
    summary.standalone_taxonomy = [
        StandaloneCategory(
            category=c["category"],
            description=c["description"],
            member_file_ids=list(c["member_file_ids"]),
        )
        for c in categories
    ]

graph.save(Path("prefxplain.json"))
total_cats = sum(len(v) for v in taxonomy_by_group.values())
print(f"Patched taxonomy for {len(taxonomy_by_group)} group(s), {total_cats} categories total")
PYEOF
```

IMPORTANT:
- Only categorize groups that have standalone files. Skip others.
- Every standalone file must land in exactly one category. Files not assigned
  to any category get a synthetic "Uncategorized" fallback in the renderer.
- If a group has standalone files but you can't find a meaningful pattern,
  use a single `"Miscellaneous"` category with all files — better than nothing.

#### 4g. Generate executive summary + health score

**Model:** The highest-leverage synthesis step in the whole run — keep
the main orchestrating model (Claude Opus 4.7 on Claude Code / Cursor,
GPT-5.4 on Codex). Never delegate 4g; the summary is what ends up in
decks, onboarding docs, and investor materials.

This is the most important step. The summary is what founders paste into decks
and what devs read to understand a project in 30 seconds.

First, collect the structural signals you need. Run:

```bash
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" -c "
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

Now look at the README too if it exists. Use the Read tool on `$REPO/README.md`
(first 30 lines). This gives you the project's own description of itself.

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
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" << 'PYEOF'
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
PPY="$HOME/.prefxplain/.venv/bin/python"; [ -x "$PPY" ] || PPY="$(command -v python3 || command -v python)"
ARTIFACT_VERSION="$(git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
ARTIFACT_DIR="$REPO/.prefxplain/$ARTIFACT_VERSION"
mkdir -p "$ARTIFACT_DIR"
printf '%s\n' "$ARTIFACT_VERSION" > "$REPO/.prefxplain/latest"
cd "$ARTIFACT_DIR" && REPO_ROOT="$REPO" "$PPY" -c "
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

If `--output` was passed in `$ARGUMENTS`, use that path instead of the default.

### 6. Preview in IDE

Open the generated HTML in the installed PrefXplain IDE preview:

```bash
ARTIFACT_VERSION="$(cat "$REPO/.prefxplain/latest" 2>/dev/null || git -C "$REPO" rev-parse --short=12 HEAD 2>/dev/null || echo working-tree)"
HTML_ABS="$REPO/.prefxplain/$ARTIFACT_VERSION/prefxplain.html"
[ -f "$HTML_ABS" ] || HTML_ABS="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$HTML_ABS")"
IDE_SCHEME="$(python3 -c "
import os
term = (os.environ.get('TERM_PROGRAM') or '').lower().strip()
generic = {'', 'apple_terminal', 'iterm.app', 'hyper', 'tmux', 'rxvt', 'xterm', 'xterm-256color', 'alacritty', 'kitty', 'ghostty', 'warp', 'wezterm'}
print('vscode' if term in generic else term)
")"
PREVIEW_URI="$(HTML_ABS="$HTML_ABS" IDE_SCHEME="$IDE_SCHEME" python3 -c "import os, urllib.parse; print(f\"{os.environ['IDE_SCHEME']}://prefxplain.prefxplain-vscode/preview?path={urllib.parse.quote(os.environ['HTML_ABS'])}\")")"

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

Then report the path:

```bash
printf '\n\033]8;;%s\033\\%s\033]8;;\033\\\n' \
  "$PREVIEW_URI" "▶ Open preview in IDE (Cmd/Ctrl+click here)"
echo ""
echo "URI:          $PREVIEW_URI"
echo "HTML on disk: $HTML_ABS"
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
  Codex / Claude Code / Copilot / Gemini panes make Markdown links reliably
  Cmd/Ctrl-clickable. Format exactly like this:

  ```
  Preview: [▶ Open in IDE]($PREVIEW_URI) ([fallback]: `prefxplain.html` at $HTML_ABS, or `Cmd+Shift+P → PrefXplain: Preview diagram`)
  ```

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

### View modes

The new default renderer is **ELK-based** (SVG, layered top→bottom, orthogonal
edge routing). It ships two user-selectable views via the switcher in the top bar:

- **Group map** (default): one hero card per architectural group. Inter-group
  arrows are aggregated with a `Nx imports` label. Best for the 30-second
  "what does this codebase do" view.
- **Nested**: groups become containers and files appear inside as mini-dashboard
  cards (role glyph + name + size dots + 2 highlights + IN/OUT fan bars).
  File-level arrows are routed orthogonally; fan-in hubs like a SPOF get a
  visual bus trunk rather than a 15-arrow comb.

Keyboard: `/` focuses the filter, `Escape` deselects, double-click on a file
card opens a 3-column Flow modal (importers → file → dependencies).

If you need the older Canvas-based layout for any reason (e.g. parity with
pre-redesign screenshots), pass `--renderer=legacy`. The old path is kept for
a transition period and will be removed in a follow-up PR.
