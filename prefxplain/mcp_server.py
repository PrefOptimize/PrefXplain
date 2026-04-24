"""MCP stdio server — exposes PrefXplain graph as AI agent tools.

Requires: pip install 'prefxplain[agent]'

Usage:
    prefxplain mcp .                     # serve graph for current directory
    prefxplain mcp . --from graph.json   # load from specific JSON path

Tools exposed:
    get_context(query, depth?, token_budget?) — BFS context dump around matching files
    get_file(file_path)                       — description, role, exports for one file
    search_files(query)                       — list matching file paths
"""

from __future__ import annotations

import sys
from pathlib import Path

from .exporter import export_agent_context
from .graph import Graph


def _default_graph_json_path(root: Path) -> Path:
    marker = root / ".prefxplain" / "latest"
    try:
        version = marker.read_text(encoding="utf-8").strip()
    except OSError:
        version = ""
    if version and "/" not in version and "\\" not in version:
        candidate = root / ".prefxplain" / version / "prefxplain.json"
        if candidate.exists():
            return candidate
    return root / "prefxplain.json"


def _load_graph(root: Path, from_json: Path | None = None) -> Graph:
    json_path = from_json or _default_graph_json_path(root)
    if not json_path.exists():
        raise FileNotFoundError(
            f"prefxplain.json not found at {json_path}. "
            "Run 'prefxplain create .' first to generate it."
        )
    return Graph.load(json_path)


def serve(root: Path, from_json: Path | None = None) -> None:
    """Start MCP stdio server. Blocks until stdin is closed."""
    try:
        from mcp import types
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
    except ImportError:
        print(
            "error: MCP server requires the 'mcp' package. Install it into "
            "the prefxplain venv with: "
            "~/.prefxplain/.venv/bin/pip install 'mcp>=1.0'",
            file=sys.stderr,
        )
        sys.exit(1)

    import asyncio

    graph = _load_graph(root, from_json)
    server = Server("prefxplain")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="get_context",
                description=(
                    "Token-efficient context about files matching a query. "
                    "Returns file descriptions, roles, exported symbols, and import edges. "
                    "Call this BEFORE reading raw files to scope what is relevant."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "File name, path fragment, or concept (e.g. 'auth', 'renderer')",
                        },
                        "depth": {
                            "type": "integer",
                            "default": 2,
                            "description": "BFS hops from matching files",
                        },
                        "token_budget": {
                            "type": "integer",
                            "default": 2000,
                            "description": "Approximate max tokens to return",
                        },
                    },
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="get_file",
                description="Description, role, and exported symbols for a specific file path.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "Relative file path from repo root (e.g. 'prefxplain/analyzer.py')",
                        }
                    },
                    "required": ["file_path"],
                },
            ),
            types.Tool(
                name="search_files",
                description="List file paths whose name or description matches a query string.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "get_context":
            text = export_agent_context(
                graph,
                query=arguments["query"],
                depth=arguments.get("depth", 2),
                token_budget=arguments.get("token_budget", 2000),
            )
        elif name == "get_file":
            node = graph.get_node(arguments["file_path"])
            if not node:
                text = f"File not found: {arguments['file_path']}"
            else:
                syms = ", ".join(f"{s.name}({s.kind})" for s in node.symbols)
                text = (
                    f"FILE {node.id}\n"
                    f"Language: {node.language}  Role: {node.role or 'unknown'}\n"
                    f"Description: {node.description or '(none)'}\n"
                    f"Exports: {syms or '(none)'}\n"
                    f"Imports {graph.outdegree(node.id)} files | "
                    f"Imported by {graph.indegree(node.id)} files"
                )
        elif name == "search_files":
            q = arguments["query"].lower()
            matches = [
                n.id for n in graph.nodes
                if q in n.id.lower() or q in n.description.lower()
            ]
            text = "\n".join(matches[:50]) if matches else "No matches."
        else:
            text = f"Unknown tool: {name}"

        return [types.TextContent(type="text", text=text)]

    asyncio.run(stdio_server(server).run())
