"""Local preview server for browser-based PrefXplain sessions.

Serves static files from a repo root and exposes a tiny same-origin API used
by the in-browser code editor to read and write files directly on disk.
"""

from __future__ import annotations

import argparse
import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse


def resolve_safe_child_path(root_dir: Path, rel: str) -> Optional[Path]:
    """Resolve a repo-relative child path, rejecting traversal and absolute paths."""
    if not rel or not isinstance(rel, str):
        return None
    candidate = Path(rel)
    if candidate.is_absolute():
        return None
    resolved_root = root_dir.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError:
        return None
    return resolved


class PreviewRequestHandler(SimpleHTTPRequestHandler):
    """Static file server with safe repo-local read/write endpoints."""

    def __init__(
        self,
        *args: object,
        directory: str | None = None,
        root_dir: str | Path | None = None,
        **kwargs: object,
    ) -> None:
        static_dir = Path(directory or root_dir or ".").resolve()
        resolved_root = Path(root_dir or static_dir).resolve()
        self._root_dir = resolved_root
        super().__init__(*args, directory=str(static_dir), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path == "/__prefxplain__/file":
            self._handle_load(parsed)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib method name
        parsed = urlparse(self.path)
        if parsed.path == "/__prefxplain__/file":
            self._handle_save()
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        """Keep the preview server quiet by default."""
        return

    def _handle_load(self, parsed) -> None:
        rel = parse_qs(parsed.query).get("path", [""])[0]
        target = resolve_safe_child_path(self._root_dir, rel)
        if not target:
            self._send_json(400, {"ok": False, "error": "invalid path"})
            return
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._send_json(404, {"ok": False, "error": "file not found"})
            return
        except OSError as error:
            self._send_json(500, {"ok": False, "error": str(error)})
            return
        self._send_json(200, {"ok": True, "content": content})

    def _handle_save(self) -> None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        try:
            raw = self.rfile.read(content_length) if content_length else b""
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return

        rel = payload.get("path", "")
        target = resolve_safe_child_path(self._root_dir, rel)
        if not target:
            self._send_json(400, {"ok": False, "error": "invalid path"})
            return
        body = payload.get("content")
        if not isinstance(body, str):
            self._send_json(400, {"ok": False, "error": "missing content"})
            return

        try:
            target.write_text(body, encoding="utf-8")
        except OSError as error:
            self._send_json(500, {"ok": False, "error": str(error)})
            return
        self._send_json(200, {"ok": True})

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve_preview(
    directory: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    root_dir: Path | None = None,
) -> None:
    """Serve generated docs with editor save/load endpoints rooted at the repo."""
    handler = partial(
        PreviewRequestHandler,
        directory=str(directory.resolve()),
        root_dir=(root_dir or directory).resolve(),
    )
    with ThreadingHTTPServer((host, port), handler) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve PrefXplain preview locally.")
    parser.add_argument(
        "--directory",
        "-d",
        default=".",
        help="Repo directory to serve. Defaults to current directory.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Bind port. Defaults to 8765.")
    args = parser.parse_args(argv)

    serve_preview(Path(args.directory), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
