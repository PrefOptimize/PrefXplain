"""PrefXplain CLI — analyze and visualize your codebase.

Usage:
    prefxplain .                        # analyze current dir, open in IDE preview
    prefxplain /path/to/repo            # analyze a specific repo
    prefxplain . --output graph.html    # write to custom path
    prefxplain . --no-descriptions      # skip LLM step (fast, offline)
    prefxplain . --format matrix        # dependency matrix view
    prefxplain . --format mermaid       # Mermaid export
    prefxplain . --filter "src/**"      # only show files matching pattern
    prefxplain . --depth 2 --focus main.py  # 2 hops around main.py
    prefxplain . --check-cycles         # exit 1 if circular deps found
    prefxplain check                    # CI rule enforcement
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import typer
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from . import __version__
from .analyzer import analyze
from .describer import describe, describe_groups
from .renderer import render, render_matrix

app = typer.Typer(
    name="prefxplain",
    help="Understand your codebase. Visual dependency maps + natural language descriptions.",
    add_completion=False,
    no_args_is_help=False,
)

console = Console()

ARTIFACT_DIRNAME = ".prefxplain"
ARTIFACT_BASENAME = "prefxplain"
LATEST_MARKER = "latest"
WORKING_TREE_VERSION = "working-tree"


def _git_commit_id(root: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def _artifact_version(root: Path) -> str:
    return _git_commit_id(root) or WORKING_TREE_VERSION


def _artifact_root(root: Path) -> Path:
    return root / ARTIFACT_DIRNAME


def _artifact_dir(root: Path, version: Optional[str] = None) -> Path:
    return _artifact_root(root) / (version or _artifact_version(root))


def _default_output_path(root: Path, ext: str) -> Path:
    return _artifact_dir(root) / f"{ARTIFACT_BASENAME}{ext}"


def _latest_marker_path(root: Path) -> Path:
    return _artifact_root(root) / LATEST_MARKER


def _valid_artifact_version(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and value not in {".", ".."}


def _write_latest_marker(root: Path, artifact_dir: Path) -> None:
    marker = _latest_marker_path(root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{artifact_dir.name}\n", encoding="utf-8")


def _read_latest_version(root: Path) -> Optional[str]:
    try:
        version = _latest_marker_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version if _valid_artifact_version(version) else None


def _latest_artifact_dir(root: Path) -> Optional[Path]:
    version = _read_latest_version(root)
    if version:
        candidate = _artifact_dir(root, version)
        if candidate.is_dir():
            return candidate
    current = _artifact_dir(root)
    return current if current.is_dir() else None


def _latest_artifact_path(root: Path, ext: str) -> Optional[Path]:
    latest_dir = _latest_artifact_dir(root)
    if not latest_dir:
        return None
    candidate = latest_dir / f"{ARTIFACT_BASENAME}{ext}"
    return candidate if candidate.exists() else None


def _default_graph_json_path(root: Path) -> Path:
    return _latest_artifact_path(root, ".json") or (root / f"{ARTIFACT_BASENAME}.json")


def _resolve_served_artifact_dir(root: Path, version: Optional[str]) -> Optional[Path]:
    if version:
        if not _valid_artifact_version(version):
            return None
        return _artifact_dir(root, version)
    return _latest_artifact_dir(root) or _artifact_dir(root)


def _print_codex_project_note() -> None:
    console.print(
        "[yellow]Codex detected, but its setup is project-local.[/yellow]"
    )
    console.print(
        "Run [bold]prefxplain setup codex[/bold] inside each repo you want to map."
    )


def _in_ide() -> bool:
    """Detect if we're running inside VS Code or JetBrains terminal."""
    return bool(
        os.environ.get("VSCODE_PID")
        or os.environ.get("TERM_PROGRAM") == "vscode"
        or os.environ.get("TERMINAL_EMULATOR") == "JetBrains-JediTerm"
    )


def _ide_uri_scheme() -> str:
    """Best-effort URI scheme for the active IDE."""
    term_program = (os.environ.get("TERM_PROGRAM") or "").lower()
    if term_program == "cursor":
        return "cursor"
    if term_program == "windsurf":
        return "windsurf"
    if term_program == "vscode-insiders":
        return "vscode-insiders"
    return "vscode"


def _open_uri(uri: str) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if os.name == "nt":
            subprocess.Popen(
                ["cmd", "/c", "start", "", uri],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        opener = shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except OSError:
        return False
    return False


def _open_ide_preview(path: Path) -> bool:
    uri = (
        f"{_ide_uri_scheme()}://prefxplain.prefxplain-vscode/preview"
        f"?path={quote(str(path.resolve()))}"
    )
    return _open_uri(uri)


def _open_output(path: Path) -> None:
    """Open the output in the IDE preview when possible, otherwise in the browser."""
    resolved = str(path.resolve())
    # Always attempt the IDE preview URI first — the OS routes vscode:// to VS Code
    # if the extension is installed, regardless of whether we're inside the IDE terminal.
    if _open_ide_preview(path):
        return
    webbrowser.open(f"file://{resolved}")


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return False


def _windows_user_profile_from_wsl() -> Optional[Path]:
    if not _is_wsl() or not shutil.which("cmd.exe") or not shutil.which("wslpath"):
        return None
    try:
        raw = subprocess.run(
            ["cmd.exe", "/C", "echo %USERPROFILE%"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if raw.returncode != 0:
        return None
    windows_path = raw.stdout.strip().strip('"')
    if not windows_path or "%" in windows_path:
        return None
    try:
        converted = subprocess.run(
            ["wslpath", "-u", windows_path],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if converted.returncode != 0:
        return None
    profile = converted.stdout.strip()
    return Path(profile) if profile else None


def _cmd_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _prefxplain_executable_for_wsl() -> str:
    candidate = Path(sys.executable).with_name("prefxplain")
    if candidate.exists():
        return str(candidate)
    return shutil.which("prefxplain") or str(candidate)


def _install_windows_shim_from_wsl(windows_profile: Optional[Path]) -> Optional[str]:
    if not windows_profile:
        return None
    shim_dir = windows_profile / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    shim_path = shim_dir / "prefxplain.cmd"
    distro = os.environ.get("WSL_DISTRO_NAME", "").strip()
    args = ["wsl.exe"]
    if distro:
        args.extend(["-d", distro])
    args.extend(["--", _prefxplain_executable_for_wsl()])
    command = " ".join(_cmd_quote(arg) for arg in args)
    try:
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_path.write_text(f"@echo off\r\n{command} %*\r\n", encoding="utf-8")
    except OSError:
        return None
    return f"Windows PATH shim (WSL): {shim_path}"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"prefxplain {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        "-v",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


@app.command(name="create")
def create(
    root: Path = typer.Argument(
        Path("."),
        help="Repository root to analyze. Defaults to current directory.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output file path. Defaults to <root>/.prefxplain/<commit>/prefxplain.<ext>.",
    ),
    no_descriptions: bool = typer.Option(
        False,
        "--no-descriptions",
        help="Skip LLM-generated descriptions (fast, works offline).",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="ANTHROPIC_API_KEY",
        help="API key (ANTHROPIC_API_KEY preferred, falls back to OPENAI_API_KEY).",
        show_default=False,
    ),
    api_base: Optional[str] = typer.Option(
        None,
        "--api-base",
        help="Custom API base URL (for Ollama, Groq, etc.).",
    ),
    ollama: bool = typer.Option(
        False,
        "--ollama",
        help="Use Ollama via its OpenAI-compatible endpoint.",
    ),
    ollama_host: Optional[str] = typer.Option(
        None,
        "--ollama-host",
        help="Ollama host/IP/domain. Only used with --ollama.",
        show_default=False,
    ),
    ollama_port: Optional[int] = typer.Option(
        None,
        "--ollama-port",
        help="Ollama port. Only used with --ollama.",
        show_default=False,
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="LLM model for descriptions. Defaults to $ANTHROPIC_MODEL, then claude-sonnet-4-6.",
    ),
    max_files: int = typer.Option(
        500,
        "--max-files",
        help="Maximum files to analyze.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the generated output in the IDE preview when available, otherwise in the browser.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Re-generate all LLM descriptions, ignoring cache.",
    ),
    detail: bool = typer.Option(
        False,
        "--detail",
        help="Generate paragraph-length descriptions instead of 1-2 sentences.",
    ),
    output_format: str = typer.Option(
        "html",
        "--format",
        help="Output format: html, matrix, mermaid, dot.",
    ),
    filter_pattern: Optional[str] = typer.Option(
        None,
        "--filter",
        help="Only include files matching this glob pattern (e.g. 'src/**/*.py').",
    ),
    focus: Optional[str] = typer.Option(
        None,
        "--focus",
        help="Focus on this file (used with --depth).",
    ),
    depth: Optional[int] = typer.Option(
        None,
        "--depth",
        help="Only show files within N hops of --focus file.",
    ),
    check_cycles: bool = typer.Option(
        False,
        "--check-cycles",
        help="Exit with code 1 if circular dependencies are found.",
    ),
    level: str = typer.Option(
        "",
        "--level",
        "-l",
        help="Audience level for descriptions: newbie, middle, strong, expert. "
             "Empty = reuse prior run's level (or 'newbie' on first run). "
             "Changing level re-describes everything in the new voice.",
    ),
    renderer_choice: str = typer.Option(
        "elk",
        "--renderer",
        help="HTML renderer: 'elk' (new SVG pipeline, default) or 'legacy' "
             "(old Canvas force-directed, kept during the transition).",
    ),
    include_config: bool = typer.Option(
        True,
        "--include-config/--no-include-config",
        help="Surface high-signal non-code files (Makefile, Dockerfile, "
             "pyproject.toml, .github/workflows/*.yml, ...) as node-only entries.",
    ),
    include_changed: bool = typer.Option(
        False,
        "--include-changed/--no-include-changed",
        help="Also include files returned by `git diff --name-only HEAD` and "
             "`git ls-files --others --exclude-standard`. Escape hatch for files "
             "you just touched that aren't yet code/config recognized.",
    ),
) -> None:
    """Analyze a codebase and generate an interactive HTML dependency graph."""
    _run(
        root=root,
        output=output,
        no_descriptions=no_descriptions,
        api_key=api_key,
        api_base=api_base,
        ollama=ollama,
        ollama_host=ollama_host,
        ollama_port=ollama_port,
        model=model,
        max_files=max_files,
        open_browser=open_browser,
        force=force,
        detail=detail,
        output_format=output_format,
        filter_pattern=filter_pattern,
        focus=focus,
        depth=depth,
        check_cycles=check_cycles,
        level=level,
        renderer_choice=renderer_choice,
        include_config=include_config,
        include_changed=include_changed,
    )


@app.command(name="update")
def update(
    root: Path = typer.Argument(
        Path("."),
        help="Repository root to re-analyze.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        help="Output HTML file path. Defaults to <root>/.prefxplain/<commit>/prefxplain.html.",
    ),
    no_descriptions: bool = typer.Option(
        False,
        "--no-descriptions",
        help="Skip LLM-generated descriptions.",
    ),
    api_key: Optional[str] = typer.Option(
        None,
        "--api-key",
        envvar="ANTHROPIC_API_KEY",
        show_default=False,
    ),
    api_base: Optional[str] = typer.Option(
        None,
        "--api-base",
    ),
    ollama: bool = typer.Option(
        False,
        "--ollama",
        help="Use Ollama via its OpenAI-compatible endpoint.",
    ),
    ollama_host: Optional[str] = typer.Option(
        None,
        "--ollama-host",
        help="Ollama host/IP/domain. Only used with --ollama.",
        show_default=False,
    ),
    ollama_port: Optional[int] = typer.Option(
        None,
        "--ollama-port",
        help="Ollama port. Only used with --ollama.",
        show_default=False,
    ),
    model: Optional[str] = typer.Option(
        None,
        "--model",
        help="LLM model for descriptions. Defaults to $ANTHROPIC_MODEL, then claude-sonnet-4-6.",
    ),
    max_files: int = typer.Option(
        500,
        "--max-files",
    ),
    open_browser: bool = typer.Option(
        False,
        "--open/--no-open",
        help="Open the generated output in the IDE preview when available, otherwise in the browser.",
    ),
    level: str = typer.Option(
        "",
        "--level",
        "-l",
        help="Audience level: newbie, middle, strong, expert. Empty = reuse prior run's level.",
    ),
    renderer_choice: str = typer.Option(
        "elk",
        "--renderer",
        help="HTML renderer: 'elk' (new SVG pipeline, default) or 'legacy' "
             "(old Canvas force-directed, kept during the transition).",
    ),
    include_config: bool = typer.Option(
        True,
        "--include-config/--no-include-config",
        help="Surface high-signal non-code files (Makefile, Dockerfile, "
             "pyproject.toml, .github/workflows/*.yml, ...) as node-only entries.",
    ),
    include_changed: bool = typer.Option(
        False,
        "--include-changed/--no-include-changed",
        help="Also include files returned by `git diff --name-only HEAD` and "
             "`git ls-files --others --exclude-standard`.",
    ),
) -> None:
    """Re-analyze the codebase. Same as `create` but defaults to not opening the browser.

    Re-analysis is fast: the SQLite cache in describer.py only re-generates
    descriptions for files whose content hash changed since the last run.
    """
    _run(
        root=root,
        output=output,
        no_descriptions=no_descriptions,
        api_key=api_key,
        api_base=api_base,
        ollama=ollama,
        ollama_host=ollama_host,
        ollama_port=ollama_port,
        model=model,
        max_files=max_files,
        open_browser=open_browser,
        force=False,
        detail=False,
        output_format="html",
        filter_pattern=None,
        focus=None,
        depth=None,
        check_cycles=False,
        level=level,
        renderer_choice=renderer_choice,
        include_config=include_config,
        include_changed=include_changed,
    )


@app.command(name="check")
def check_cmd(
    root: Path = typer.Argument(
        Path("."),
        help="Repository root to check.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    config: Path = typer.Option(
        None,
        "--config",
        "-c",
        help="Path to .prefxplain.yml config. Defaults to <root>/.prefxplain.yml.",
    ),
    max_files: int = typer.Option(
        500,
        "--max-files",
    ),
) -> None:
    """Check dependency rules (CI enforcement). Exits 1 on violations."""
    from .checker import check, format_violations, load_rules

    config_path = config or (root / ".prefxplain.yml")
    if not config_path.exists():
        console.print(f"[red]Config not found: {config_path}[/red]")
        console.print("Create a .prefxplain.yml with rules. Example:")
        console.print("[dim]rules:[/dim]")
        console.print("[dim]  - name: no-circular-deps[/dim]")
        console.print("[dim]  - name: max-imports[/dim]")
        console.print("[dim]    max: 10[/dim]")
        raise typer.Exit(1)

    console.print(f"[bold]PrefXplain Check[/bold] — {root}")
    rules = load_rules(config_path)
    console.print(f"  Loaded {len(rules)} rule(s) from {config_path.name}")

    graph = analyze(root, max_files=max_files)
    console.print(f"  Analyzed {len(graph.nodes)} files, {len(graph.edges)} edges")

    violations = check(graph, rules)
    console.print(format_violations(violations))

    errors = [v for v in violations if v.severity == "error"]
    if errors:
        raise typer.Exit(1)


@app.command(name="context")
def context_cmd(
    query: str = typer.Argument(..., help="Search term — file name, path, or concept"),
    root: Path = typer.Argument(
        Path("."),
        help="Repository root. Defaults to current directory.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    depth: int = typer.Option(2, "--depth", "-d", help="BFS hops around matching files"),
    tokens: int = typer.Option(2000, "--tokens", "-t", help="Approximate token budget for output"),
    from_json: Optional[Path] = typer.Option(
        None,
        "--from",
        help="Load from prefxplain.json (default: <root>/.prefxplain/latest target, fallback <root>/prefxplain.json).",
    ),
    max_files: int = typer.Option(500, "--max-files"),
) -> None:
    """Output token-efficient context for AI agents. Loads from prefxplain.json when available."""
    from .exporter import export_agent_context

    json_path = from_json or _default_graph_json_path(root)
    if json_path.exists():
        from .graph import Graph as _Graph
        graph = _Graph.load(json_path)
    else:
        console.print("[yellow]prefxplain.json not found — analyzing from source...[/yellow]")
        graph = analyze(root, max_files=max_files)
        graph.infer_roles()

    print(export_agent_context(graph, query, depth=depth, token_budget=tokens))


@app.command(name="mcp")
def mcp_cmd(
    root: Path = typer.Argument(
        Path("."),
        help="Repository root. Defaults to current directory.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    from_json: Optional[Path] = typer.Option(
        None,
        "--from",
        help="Load from prefxplain.json (default: <root>/.prefxplain/latest target, fallback <root>/prefxplain.json).",
    ),
) -> None:
    """Start MCP stdio server for AI agent integration. Requires the `mcp` package: `~/.prefxplain/.venv/bin/pip install 'mcp>=1.0'`."""
    from .mcp_server import serve
    serve(root, from_json or _default_graph_json_path(root))


@app.command(name="serve")
def serve_cmd(
    root: Path = typer.Argument(
        Path("."),
        help="Repository root whose generated docs should be served.",
        exists=True,
        file_okay=False,
        dir_okay=True,
        resolve_path=True,
    ),
    version: Optional[str] = typer.Option(
        None,
        "--version",
        help="Generated doc version to serve. Defaults to the .prefxplain/latest marker.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind host.",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        "-p",
        help="Bind port.",
    ),
    open_browser: bool = typer.Option(
        True,
        "--open/--no-open",
        help="Open the served preview URL before blocking.",
    ),
) -> None:
    """Serve the latest generated PrefXplain docs from .prefxplain/<version>."""
    artifact_dir = _resolve_served_artifact_dir(root, version)
    if not artifact_dir:
        console.print(f"[red]Invalid PrefXplain doc version: {version}[/red]")
        raise typer.Exit(1)

    html_path = artifact_dir / f"{ARTIFACT_BASENAME}.html"
    if not html_path.exists():
        console.print(f"[red]Generated docs not found: {html_path}[/red]")
        console.print("Run [bold]prefxplain create .[/bold] first, or pass [bold]--version[/bold].")
        raise typer.Exit(1)

    url = f"http://{host}:{port}/{ARTIFACT_BASENAME}.html"
    console.print(f"[bold]Serving PrefXplain docs[/bold] from [cyan]{artifact_dir}[/cyan]")
    console.print(f"[dim]{url}[/dim]")
    if open_browser and not _open_uri(url):
        webbrowser.open(url)

    from .preview_server import serve_preview

    serve_preview(artifact_dir, host=host, port=port, root_dir=root)


@app.command(name="upgrade")
def upgrade_cmd() -> None:
    """Upgrade prefxplain to the latest version from GitHub main.

    Re-runs the official curl|bash installer, which wipes ``~/.prefxplain`` and
    reclones a fresh copy from https://github.com/PrefOptimize/PrefXplain. This
    is the canonical update path — PyPI is intentionally not used because it
    lags GitHub main.
    """
    installer_url = (
        "https://raw.githubusercontent.com/PrefOptimize/PrefXplain/main/install.sh"
    )
    if not shutil.which("curl"):
        console.print("[red]curl not found on PATH. Install curl and retry.[/red]")
        raise typer.Exit(1)
    if not shutil.which("bash"):
        console.print("[red]bash not found on PATH. Install bash and retry.[/red]")
        raise typer.Exit(1)

    console.print("[bold]Upgrading prefxplain…[/bold]")
    console.print(f"[dim]Source: {installer_url}[/dim]")
    console.print()

    cmd = f"curl -fsSL {installer_url} | bash"
    try:
        result = subprocess.run(["bash", "-c", cmd], check=False)
    except OSError as exc:
        console.print(f"[red]Failed to launch installer: {exc}[/red]")
        raise typer.Exit(1) from exc

    if result.returncode != 0:
        console.print(
            f"[red]Installer exited with code {result.returncode}. "
            "Check the output above for details.[/red]"
        )
        raise typer.Exit(result.returncode)

    console.print()
    console.print("[bold green]Upgrade complete.[/bold green]")
    console.print(
        "[dim]If `which prefxplain` still points elsewhere, remove any pip- or "
        "conda-installed copy and ensure ~/.local/bin is ahead on PATH.[/dim]"
    )


@app.command(name="setup")
def setup_cmd(
    tool: Optional[str] = typer.Argument(
        None,
        help="AI tool to set up: claude-code, cursor, codex, copilot, gemini. Auto-detects if omitted.",
    ),
    project: bool = typer.Option(
        False,
        "--project",
        "-p",
        help="Install to current project only (not global).",
    ),
) -> None:
    """Install the /prefxplain slash command for your AI coding tool.

    Auto-detects Claude Code, Cursor, Codex, Copilot CLI, and Gemini CLI. Use
    --project to install for the current repo only instead of globally.
    """
    package_root = Path(__file__).parent
    cmd_source = package_root / "commands" / "prefxplain.md"
    update_cmd_source = package_root / "commands" / "prefxplain-update.md"
    # Per-file description worker — Haiku 4.5 on Claude Code, gpt-5.4-mini on
    # Codex. Without these, step 4c of /prefxplain has no cheap/fast subagent
    # to dispatch to and silently falls back to the main model (10× cost,
    # serial).
    claude_worker_source = package_root / "agents" / "prefxplain-worker.md"
    codex_worker_source = package_root / "agents" / "prefxplain-worker.toml"
    cmd_content: Optional[str] = None
    update_cmd_content: Optional[str] = None
    copilot_plugin_dir = package_root / "copilot_plugin"
    copilot_plugin_manifest = copilot_plugin_dir / "plugin.json"
    # The Copilot plugin's SKILL.md is a plain Anthropic-style Agent Skill
    # (universal format — also works for Gemini CLI, Cursor, and future tools).
    agent_skill_source = copilot_plugin_dir / "skills" / "prefxplain" / "SKILL.md"

    def _load_cmd_content() -> str:
        nonlocal cmd_content
        if cmd_content is None:
            if not cmd_source.exists():
                console.print("[red]Command template not found in package. Reinstall prefxplain.[/red]")
                raise typer.Exit(1)
            cmd_content = cmd_source.read_text(encoding="utf-8")
        return cmd_content

    def _load_update_cmd_content() -> Optional[str]:
        """Lazy-load the /prefxplain-update slash command. Returns None if missing.

        Absence is non-fatal — older installs may not ship this file yet, and we
        don't want a missing update command to block the main setup.
        """
        nonlocal update_cmd_content
        if update_cmd_content is None and update_cmd_source.exists():
            update_cmd_content = update_cmd_source.read_text(encoding="utf-8")
        return update_cmd_content

    # Detect available tools
    detected: list[str] = []
    home = Path.home()
    windows_profile = None if project else _windows_user_profile_from_wsl()
    installed: list[str] = []
    if not project:
        shim_result = _install_windows_shim_from_wsl(windows_profile)
        if shim_result:
            installed.append(shim_result)

    if (
        (home / ".claude").is_dir()
        or shutil.which("claude")
        or (windows_profile is not None and (windows_profile / ".claude").is_dir())
    ):
        detected.append("claude-code")
    if (
        (home / ".cursor").is_dir()
        or shutil.which("cursor")
        or (windows_profile is not None and (windows_profile / ".cursor").is_dir())
    ):
        detected.append("cursor")
    codex_available = bool(shutil.which("codex"))
    if codex_available and project:
        detected.append("codex")
    if shutil.which("copilot"):
        detected.append("copilot")
    if (home / ".gemini").is_dir() or shutil.which("gemini"):
        detected.append("gemini")

    if not project:
        ext_result = _install_vscode_extension(package_root)
        if ext_result:
            installed.append(ext_result)

    targets = [tool] if tool else detected
    if not targets:
        if not tool and not project and codex_available:
            if installed:
                console.print("[bold green]Setup complete![/bold green]")
                for item in installed:
                    console.print(f"  [green]\u2713[/green] {item}")
                console.print()
            _print_codex_project_note()
            return
        if installed:
            console.print("[bold green]Setup complete![/bold green]")
            for item in installed:
                console.print(f"  [green]\u2713[/green] {item}")
            console.print()
            console.print(
                "[yellow]No AI coding tools detected, so /prefxplain was not registered yet.[/yellow]"
            )
            console.print("Supported: claude-code, cursor, codex, copilot, gemini")
            console.print(
                "Run: [bold]prefxplain setup gemini[/bold] (or copilot, claude-code, ...) to install manually."
            )
            return
        console.print("[yellow]No AI coding tools detected.[/yellow]")
        console.print("Supported: claude-code, cursor, codex, copilot, gemini")
        console.print("Run: [bold]prefxplain setup gemini[/bold] (or copilot, claude-code, ...) to install manually.")
        raise typer.Exit(1)

    failed = False
    for t in targets:
        if t == "claude-code":
            claude_root = (
                Path.cwd() / ".claude" if project else home / ".claude"
            )
            commands_dir = claude_root / "commands"
            agents_dir = claude_root / "agents"
            commands_dir.mkdir(parents=True, exist_ok=True)
            scope = "project" if project else "global"

            dest = commands_dir / "prefxplain.md"
            dest.write_text(_load_cmd_content(), encoding="utf-8")
            installed.append(f"Claude Code ({scope}): {dest}")

            update_body = _load_update_cmd_content()
            if update_body is not None:
                update_dest = commands_dir / "prefxplain-update.md"
                update_dest.write_text(update_body, encoding="utf-8")
                installed.append(f"Claude Code ({scope}): {update_dest}")

            # Ship the Haiku worker agent so step 4c's parallel dispatch
            # actually has a cheap/fast subagent to target. Missing this
            # file is what caused the "it ran everything inline on Opus"
            # bug — Claude Code silently falls back to the main model when
            # the named subagent_type doesn't exist.
            if claude_worker_source.exists():
                agents_dir.mkdir(parents=True, exist_ok=True)
                worker_dest = agents_dir / "prefxplain-worker.md"
                worker_dest.write_text(
                    claude_worker_source.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                installed.append(f"Claude Code ({scope}): {worker_dest}")

            if not project and windows_profile is not None:
                win_claude_root = windows_profile / ".claude"
                win_commands_dir = win_claude_root / "commands"
                win_agents_dir = win_claude_root / "agents"
                win_commands_dir.mkdir(parents=True, exist_ok=True)

                win_dest = win_commands_dir / "prefxplain.md"
                win_dest.write_text(_load_cmd_content(), encoding="utf-8")
                installed.append(f"Claude Code (Windows via WSL): {win_dest}")

                update_body = _load_update_cmd_content()
                if update_body is not None:
                    win_update_dest = win_commands_dir / "prefxplain-update.md"
                    win_update_dest.write_text(update_body, encoding="utf-8")
                    installed.append(f"Claude Code (Windows via WSL): {win_update_dest}")

                if claude_worker_source.exists():
                    win_agents_dir.mkdir(parents=True, exist_ok=True)
                    win_worker_dest = win_agents_dir / "prefxplain-worker.md"
                    win_worker_dest.write_text(
                        claude_worker_source.read_text(encoding="utf-8"),
                        encoding="utf-8",
                    )
                    installed.append(f"Claude Code (Windows via WSL): {win_worker_dest}")

        elif t == "cursor":
            # Cursor uses .cursor/rules/ for project-level instructions
            rules_dir = (
                Path.cwd() / ".cursor" / "rules"
                if project
                else home / ".cursor" / "rules"
            )
            rules_dir.mkdir(parents=True, exist_ok=True)
            scope = "project" if project else "global"

            dest = rules_dir / "prefxplain.mdc"
            # Cursor uses .mdc format — wrap the command as a rule
            cursor_content = (
                "---\n"
                "description: Generate interactive dependency graph with /prefxplain\n"
                "globs: \n"
                "alwaysApply: false\n"
                "---\n\n"
                + _load_cmd_content()
            )
            dest.write_text(cursor_content, encoding="utf-8")
            installed.append(f"Cursor ({scope}): {dest}")

            update_body = _load_update_cmd_content()
            if update_body is not None:
                update_dest = rules_dir / "prefxplain-update.mdc"
                cursor_update = (
                    "---\n"
                    "description: Upgrade prefxplain itself to the latest GitHub main\n"
                    "globs: \n"
                    "alwaysApply: false\n"
                    "---\n\n"
                    + update_body
                )
                update_dest.write_text(cursor_update, encoding="utf-8")
                installed.append(f"Cursor ({scope}): {update_dest}")

            if not project and windows_profile is not None:
                win_rules_dir = windows_profile / ".cursor" / "rules"
                win_rules_dir.mkdir(parents=True, exist_ok=True)

                win_dest = win_rules_dir / "prefxplain.mdc"
                win_dest.write_text(cursor_content, encoding="utf-8")
                installed.append(f"Cursor (Windows via WSL): {win_dest}")

                update_body = _load_update_cmd_content()
                if update_body is not None:
                    win_update_dest = win_rules_dir / "prefxplain-update.mdc"
                    win_cursor_update = (
                        "---\n"
                        "description: Upgrade prefxplain itself to the latest GitHub main\n"
                        "globs: \n"
                        "alwaysApply: false\n"
                        "---\n\n"
                        + update_body
                    )
                    win_update_dest.write_text(win_cursor_update, encoding="utf-8")
                    installed.append(f"Cursor (Windows via WSL): {win_update_dest}")

        elif t == "codex":
            # Codex uses AGENTS.md for project instructions. There is no global
            # install target for the slash-command side, so explicit setup
            # always writes to the current repo.
            dest = Path.cwd() / "AGENTS.md"
            section = (
                "\n\n## /prefxplain\n\n"
                "When the user asks to map, visualize, or explain the codebase architecture, "
                "run `prefxplain .` to generate an interactive HTML dependency graph.\n"
            )
            if dest.exists():
                existing = dest.read_text(encoding="utf-8")
                if "prefxplain" not in existing:
                    dest.write_text(existing + section, encoding="utf-8")
                    installed.append(f"Codex (project): appended to {dest}")
                else:
                    installed.append(f"Codex (project): already configured in {dest}")
            else:
                dest.write_text(f"# Agent Instructions{section}", encoding="utf-8")
                installed.append(f"Codex (project): created {dest}")

            # The Codex agent profile itself lives under ~/.codex/agents/ and
            # is read globally by the CLI regardless of which repo invoked it.
            # We always install it there so `spawn_agent("prefxplain_worker")`
            # resolves to gpt-5.4-mini instead of silently reusing GPT-5.4.
            if codex_worker_source.exists():
                codex_agents_dir = home / ".codex" / "agents"
                codex_agents_dir.mkdir(parents=True, exist_ok=True)
                worker_dest = codex_agents_dir / "prefxplain-worker.toml"
                worker_dest.write_text(
                    codex_worker_source.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
                installed.append(f"Codex (global): {worker_dest}")

        elif t == "copilot":
            if not copilot_plugin_manifest.exists():
                console.print(
                    "[red]Copilot plugin assets missing from package. Reinstall prefxplain.[/red]"
                )
                failed = True
                if tool == "copilot":
                    raise typer.Exit(1)
                console.print("[yellow]Skipping Copilot setup.[/yellow]")
                continue

            copilot_bin = shutil.which("copilot")
            if not copilot_bin:
                console.print("[red]copilot CLI not found on PATH.[/red]")
                console.print(
                    "Install GitHub Copilot CLI, then run: [bold]prefxplain setup copilot[/bold]."
                )
                failed = True
                if tool == "copilot":
                    raise typer.Exit(1)
                console.print("[yellow]Skipping Copilot setup.[/yellow]")
                continue

            if project:
                console.print(
                    "[yellow]Copilot setup uses global plugins; ignoring --project.[/yellow]"
                )

            # `copilot plugin install` can take 3-4 minutes on a fresh machine
            # (npm fetch + validation). In auto-detect mode, ask first so the
            # user can opt out and keep going. In explicit mode (`prefxplain
            # setup copilot`), assume opt-in and just run it. Skip the prompt
            # entirely when stdin isn't a TTY (e.g. an LLM driving ./setup
            # via the Bash tool can't answer prompts).
            if tool != "copilot":
                # Visual separation + header so the prompt is anchored to the
                # Copilot step, not floating with no context.
                console.print()
                console.print(
                    "[bold]\u25b8 Copilot CLI plugin[/bold]"
                    " [dim](optional, slow on a fresh machine)[/dim]"
                )
                if not _stdin_is_interactive():
                    console.print(
                        "  [dim]Non-interactive shell — skipped."
                        " Run [bold]prefxplain setup copilot[/bold] later to enable it.[/dim]"
                    )
                    continue
                if not typer.confirm(
                    "  Installing the plugin can take 3-4 min. Install now?",
                    default=False,
                ):
                    console.print(
                        "  [dim]Skipped. Run [bold]prefxplain setup copilot[/bold]"
                        " later to enable it.[/dim]"
                    )
                    continue
                console.print("  [dim]Installing… streaming Copilot CLI output below.[/dim]")

            # The user has explicitly opted in (either via the y/N prompt or
            # by running `prefxplain setup copilot` directly). Don't impose an
            # artificial timeout — `copilot plugin install` can legitimately
            # take 5-15 min on slow VMs while npm fetches dependencies. Stream
            # stdout/stderr live so they see actual progress instead of a
            # silent "is it stuck?" wait.
            try:
                returncode = subprocess.call(
                    [copilot_bin, "plugin", "install", str(copilot_plugin_dir)],
                )
            except KeyboardInterrupt:
                console.print(
                    "[yellow]\u26a0[/yellow] Copilot plugin install cancelled."
                    " Re-run [bold]prefxplain setup copilot[/bold] later."
                )
                if tool == "copilot":
                    raise typer.Exit(130)
                continue
            if returncode != 0:
                console.print("[red]Failed to install Copilot plugin.[/red]")
                failed = True
                if tool == "copilot":
                    raise typer.Exit(1)
                console.print("[yellow]Skipping Copilot setup.[/yellow]")
                continue

            installed.append(f"Copilot CLI (global plugin): {copilot_plugin_manifest}")

        elif t == "gemini":
            if not agent_skill_source.exists():
                console.print(
                    "[red]Agent skill asset missing from package. Reinstall prefxplain.[/red]"
                )
                failed = True
                if tool == "gemini":
                    raise typer.Exit(1)
                console.print("[yellow]Skipping Gemini setup.[/yellow]")
                continue

            if project:
                dest = Path.cwd() / ".gemini" / "skills" / "prefxplain" / "SKILL.md"
            else:
                dest = home / ".gemini" / "skills" / "prefxplain" / "SKILL.md"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(agent_skill_source.read_text(encoding="utf-8"), encoding="utf-8")
            scope = "project" if project else "global"
            installed.append(f"Gemini CLI ({scope}): {dest}")

        else:
            console.print(f"[yellow]Unknown tool: {t}. Skipping.[/yellow]")

    if installed:
        console.print("[bold green]Setup complete![/bold green]")
        for item in installed:
            console.print(f"  [green]\u2713[/green] {item}")
        console.print()
        if not tool and not project and codex_available:
            _print_codex_project_note()
            console.print()
        console.print("Now type [bold]/prefxplain[/bold] in your AI tool to generate a codebase map.")
        console.print(
            "Type [bold]/prefxplain-update[/bold] whenever you want to pull the latest renderer."
        )
    else:
        console.print("[yellow]Nothing to install.[/yellow]")
        if failed:
            raise typer.Exit(1)


def _stdin_is_interactive() -> bool:
    """Return True if stdin is a TTY (i.e. a real human can answer prompts).

    Wrapped in a helper so tests can monkeypatch it cleanly — Click's
    CliRunner swaps `sys.stdin` for a StringIO during `invoke()`, which
    makes patching `sys.stdin.isatty` directly unreliable.
    """
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _install_vscode_extension(package_root: Path) -> Optional[str]:
    """Install the PrefXplain preview extension into VS Code/Cursor/Windsurf.

    Looks for a pre-built `.vsix` in two places (in order):
      1. <package_root>/vscode_extension/*.vsix        (shipped in the wheel)
      2. <repo_root>/prefxplain-vscode/*.vsix          (editable install from
         a git clone, produced by `./setup` or `make install-extension`)

    Returns a human-readable status line on success, or None if skipped.
    Best-effort: any failure (no .vsix, no IDE CLI, install error) is silent
    so `setup` never fails because of the optional extension.
    """
    candidates = [
        package_root / "vscode_extension",
        package_root.parent / "prefxplain-vscode",
    ]
    vsix_path: Optional[Path] = None
    for d in candidates:
        if d.is_dir():
            matches = sorted(d.glob("*.vsix"))
            if matches:
                vsix_path = matches[-1]
                break
    if vsix_path is None:
        return None

    # VS Code family: the extension is the same `.vsix` for all forks, since
    # Cursor/Windsurf/Antigravity/Trae/Void/VSCodium/Positron all inherit
    # VS Code's extension API. Key: CLI binary name (used by
    # `<cli> --install-extension`). Value: (display name, macOS app-bundle
    # fallback path for when the CLI isn't shimmed onto PATH).
    ide_catalog: list[tuple[str, str, Optional[Path]]] = [
        ("code",          "VS Code",          Path("/Applications/Visual Studio Code.app/Contents/Resources/app/bin/code")),
        ("code-insiders", "VS Code Insiders", Path("/Applications/Visual Studio Code - Insiders.app/Contents/Resources/app/bin/code")),
        ("cursor",        "Cursor",           Path("/Applications/Cursor.app/Contents/Resources/app/bin/cursor")),
        ("windsurf",      "Windsurf",         Path("/Applications/Windsurf.app/Contents/Resources/app/bin/windsurf")),
        ("antigravity",   "Antigravity",      Path("/Applications/Antigravity.app/Contents/Resources/app/bin/antigravity")),
        ("trae",          "Trae",             Path("/Applications/Trae.app/Contents/Resources/app/bin/trae")),
        ("void",          "Void",             Path("/Applications/Void.app/Contents/Resources/app/bin/void")),
        ("vscodium",      "VSCodium",         Path("/Applications/VSCodium.app/Contents/Resources/app/bin/codium")),
        ("codium",        "VSCodium",         None),
        ("positron",      "Positron",         Path("/Applications/Positron.app/Contents/Resources/app/bin/positron")),
    ]
    targets: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for cli_name, display, mac_bundle in ide_catalog:
        path = shutil.which(cli_name)
        if not path and mac_bundle is not None and mac_bundle.exists():
            path = str(mac_bundle)
        if path and path not in seen_paths:
            seen_paths.add(path)
            targets.append((display, path))
    if not targets:
        return None

    installed_in: list[str] = []
    for name, path in targets:
        try:
            result = subprocess.run(
                [path, "--install-extension", str(vsix_path), "--force"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode == 0:
            installed_in.append(name)

    if not installed_in:
        return None
    return f"Preview extension ({', '.join(installed_in)}): {vsix_path.name}"


def _run(
    root: Path,
    output: Optional[Path],
    no_descriptions: bool,
    api_key: Optional[str],
    api_base: Optional[str],
    ollama: bool,
    ollama_host: Optional[str],
    ollama_port: Optional[int],
    model: str,
    max_files: int,
    open_browser: bool,
    force: bool,
    detail: bool,
    output_format: str,
    filter_pattern: Optional[str],
    focus: Optional[str],
    depth: Optional[int],
    check_cycles: bool,
    level: str = "",
    renderer_choice: str = "elk",
    include_config: bool = True,
    include_changed: bool = False,
) -> None:
    """Shared implementation for create and update commands.

    Both commands run the full pipeline: analyze, describe, render. Re-runs are
    fast because describer.py caches by (file_path, content_hash, level) in SQLite.
    A change in `level` invalidates prior descriptions so the new voice actually
    shows up on re-run.
    """
    from .describer import _DEFAULT_LEVEL, VALID_LEVELS

    # Resolve the requested level: CLI arg > prior run's level (from JSON) > default.
    requested_level = (level or "").strip().lower()
    if requested_level and requested_level not in VALID_LEVELS:
        console.print(
            f"[yellow]Unknown level '{level}'. Using '{_DEFAULT_LEVEL}'. "
            f"Valid: {', '.join(sorted(VALID_LEVELS))}.[/yellow]"
        )
        requested_level = ""
    fmt = output_format.lower()
    default_ext = {
        "html": ".html",
        "matrix": ".html",
        "mermaid": ".md",
        "dot": ".dot",
    }
    ext = default_ext.get(fmt, ".html")
    default_output = output is None
    output_path = output or _default_output_path(root, ext)

    console.print(
        Panel(
            Text.from_markup(
                f"[bold]PrefXplain[/bold] [dim]v{__version__}[/dim]\n"
                f"Analyzing [cyan]{root}[/cyan]"
            ),
            expand=False,
        )
    )

    # Step 1: Static analysis
    console.print("[bold blue]1/3[/bold blue] Parsing files and extracting imports...")
    graph = analyze(
        root,
        max_files=max_files,
        include_config=include_config,
        include_changed=include_changed,
    )

    # Apply filter
    if filter_pattern:
        graph = graph.filter_subgraph(filter_pattern)
        console.print(f"    [dim]Filtered to {len(graph.nodes)} files matching '{filter_pattern}'[/dim]")

    # Apply depth focus
    if focus and depth is not None:
        graph = graph.depth_subgraph(focus, depth)
        console.print(f"    [dim]Focused on {len(graph.nodes)} files within {depth} hops of '{focus}'[/dim]")
    elif focus and depth is None:
        console.print("[yellow]    \u26a0 --focus requires --depth. Ignoring --focus.[/yellow]")

    # Infer architectural roles
    graph.infer_roles()
    graph.infer_groups()

    console.print(
        f"    [green]\u2713[/green] Found {len(graph.nodes)} files, {len(graph.edges)} import edges"
        f" ({', '.join(graph.metadata.languages or ['?'])})"
    )

    # Report cycles
    cycles = graph.find_cycles()
    if cycles:
        console.print(
            f"    [yellow]\u26a0[/yellow] {len(cycles)} circular dependency chain(s) detected"
        )
        for cycle in cycles[:3]:
            chain = " \u2192 ".join(cycle) + " \u2192 " + cycle[0]
            console.print(f"      [dim]{chain}[/dim]")
        if len(cycles) > 3:
            console.print(f"      [dim]+{len(cycles) - 3} more[/dim]")

    # Step 2: LLM descriptions
    # Always preserve prior descriptions first — so a failed LLM call or
    # --no-descriptions never silently wipes descriptions from a previous run.
    # BUT: if the requested audience level differs from the prior run's level,
    # skip preservation so the new voice actually takes effect.
    prior_json_path = output_path.with_suffix(".json")
    if default_output and not prior_json_path.exists():
        prior_json_path = _latest_artifact_path(root, ".json") or prior_json_path
    prior_level = ""
    if prior_json_path.exists():
        try:
            from .diagram import is_generated_group_label
            from .graph import Graph as _Graph

            prior_graph = _Graph.load(prior_json_path)
            prior_level = (getattr(prior_graph.metadata, "level", "") or "").strip().lower()

            # Resolve effective level: explicit CLI arg wins; else reuse prior; else default.
            effective_level = requested_level or prior_level or _DEFAULT_LEVEL

            level_changed = bool(
                requested_level and prior_level and requested_level != prior_level
            )
            if level_changed:
                console.print(
                    f"    [yellow]Level changed ({prior_level} \u2192 {requested_level})"
                    f" — re-describing in the new voice.[/yellow]"
                )
                # Architecture doesn't change with voice — keep group
                # assignments and the groups→description map so the LLM
                # doesn't have to re-derive them on every voice change.
                prior_by_id = {n.id: n for n in prior_graph.nodes}
                for node in graph.nodes:
                    prior = prior_by_id.get(node.id)
                    if prior and prior.group and not is_generated_group_label(prior.group):
                        node.group = prior.group
                if prior_graph.metadata.groups:
                    graph.metadata.groups = dict(prior_graph.metadata.groups)
            else:
                prior_descs = {n.id: n for n in prior_graph.nodes if n.description}
                preserved = 0
                for node in graph.nodes:
                    prior = prior_descs.get(node.id)
                    if prior:
                        node.description = prior.description
                        if prior.short_title:
                            node.short_title = prior.short_title
                        if prior.flowchart:
                            node.flowchart = prior.flowchart
                        if prior.highlights:
                            node.highlights = list(prior.highlights)
                        # v3 semantic fields — carry over so re-runs without
                        # --no-descriptions or under --no-descriptions keep the
                        # enriched scaffolding instead of losing it on every render.
                        for field_name in ("semantic_role", "flow", "extends_at", "pattern"):
                            value = getattr(prior, field_name, "")
                            if value:
                                setattr(node, field_name, value)
                        # File Brief fields — preserve just like semantic_role
                        # so invariants and watch_if_changed survive re-runs.
                        prior_invariants = list(getattr(prior, "invariants", []) or [])
                        if prior_invariants:
                            node.invariants = prior_invariants
                        prior_watch = list(getattr(prior, "watch_if_changed", []) or [])
                        if prior_watch:
                            node.watch_if_changed = prior_watch
                        # Also restore per-symbol descriptions
                        old_sym = {s.name: s.description for s in prior.symbols if s.description}
                        for sym in node.symbols:
                            if sym.name in old_sym:
                                sym.description = old_sym[sym.name]
                        if prior.group and not is_generated_group_label(prior.group):
                            node.group = prior.group
                        preserved += 1
                # Carry over graph-level metadata that the skill/API path populated
                # on the previous run. Without these, a re-run wipes user-written
                # summaries and health notes.
                if prior_graph.metadata.summary:
                    graph.metadata.summary = prior_graph.metadata.summary
                if prior_graph.metadata.health_score:
                    graph.metadata.health_score = prior_graph.metadata.health_score
                if prior_graph.metadata.health_notes:
                    graph.metadata.health_notes = prior_graph.metadata.health_notes
                if prior_graph.metadata.group_highlights:
                    graph.metadata.group_highlights = dict(prior_graph.metadata.group_highlights)
                if prior_graph.metadata.group_summaries:
                    graph.metadata.group_summaries = dict(prior_graph.metadata.group_summaries)
                if preserved:
                    console.print(
                        f"    [dim]Preserved {preserved} description(s) from previous run[/dim]"
                    )
        except Exception:  # noqa: BLE001 — graceful degradation
            effective_level = requested_level or _DEFAULT_LEVEL
    else:
        effective_level = requested_level or _DEFAULT_LEVEL

    graph.metadata.level = effective_level

    if no_descriptions or fmt in ("mermaid", "dot"):
        console.print("[bold blue]2/3[/bold blue] Skipping descriptions (preserved from cache)")
    else:
        console.print(
            f"[bold blue]2/3[/bold blue] Generating natural language descriptions "
            f"[dim](level: {effective_level})[/dim]..."
        )
        # When the level changed, force re-description so the new voice applies.
        should_force = force or (
            requested_level and prior_level and requested_level != prior_level
        )
        graph = describe(
            graph,
            root=root,
            api_key=api_key,
            api_base=api_base,
            ollama=ollama,
            ollama_host=ollama_host,
            ollama_port=ollama_port,
            model=model,
            force=should_force,
            detail=detail,
            level=effective_level,
        )
        if not detail:
            graph = describe_groups(
                graph,
                root=root,
                api_key=api_key,
                api_base=api_base,
                ollama=ollama,
                ollama_host=ollama_host,
                ollama_port=ollama_port,
                model=model,
                force=should_force,
                level=effective_level,
            )

    # Step 3: Render output
    console.print(f"[bold blue]3/3[/bold blue] Rendering {fmt} output...")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "matrix":
        render_matrix(graph, output_path=output_path)
    elif fmt == "mermaid":
        from .exporter import export_mermaid
        mermaid_str = export_mermaid(graph)
        content = f"```mermaid\n{mermaid_str}```\n"
        output_path.write_text(content, encoding="utf-8")
    elif fmt == "dot":
        from .exporter import export_dot
        dot_str = export_dot(graph)
        output_path.write_text(dot_str, encoding="utf-8")
    else:
        render(graph, output_path=output_path, renderer=renderer_choice)

    console.print(f"    [green]\u2713[/green] Written to [cyan]{output_path}[/cyan]")

    # Save graph.json alongside for programmatic access
    json_path = output_path.with_suffix(".json")
    graph.save(json_path)
    console.print(f"    [green]\u2713[/green] Graph data at [cyan]{json_path}[/cyan]")
    if default_output:
        _write_latest_marker(root, output_path.parent)

    # Print metrics summary
    metrics = graph.metrics()
    console.print()
    console.print(
        f"[bold green]Done![/bold green] {metrics['total_files']} files, "
        f"{metrics['total_edges']} edges, "
        f"{metrics['components']} component(s), "
        f"{metrics['cycles']} cycle(s)"
    )

    if open_browser and fmt in ("html", "matrix"):
        _open_output(output_path)

    if check_cycles and cycles:
        console.print(f"\n[red bold]FAIL:[/red bold] {len(cycles)} circular dependency chain(s) found.")
        raise typer.Exit(1)


def entry_point() -> None:
    """Entry point that handles bare `prefxplain <path>` without a subcommand.

    If the first arg looks like a path or is missing, prepend `create` so typer
    routes correctly. This makes `prefxplain .` work as shorthand for
    `prefxplain create .`.
    """
    args = sys.argv[1:]
    subcommands = {
        "create",
        "update",
        "upgrade",
        "check",
        "context",
        "mcp",
        "serve",
        "setup",
        "--help",
        "-h",
        "--version",
        "-v",
    }
    if args and args[0] not in subcommands:
        sys.argv.insert(1, "create")
    app()


if __name__ == "__main__":
    entry_point()
