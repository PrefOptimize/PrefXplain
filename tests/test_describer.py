"""Tests for the LLM description generator and cache."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from prefxplain.describer import (
    ANTHROPIC_BASE,
    _content_hash,
    _file_preview,
    _get_cached,
    _init_cache,
    _make_prompt,
    _resolve_client_config,
    _set_cached,
    describe,
)
from prefxplain.graph import Graph, GraphMetadata, Node, Symbol

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def graph_with_nodes(tmp_path: Path) -> tuple[Graph, Path]:
    """Create a graph with real files on disk for testing."""
    (tmp_path / "main.py").write_text("def run():\n    pass\n")
    (tmp_path / "utils.py").write_text("def helper():\n    return 42\n")

    graph = Graph(
        metadata=GraphMetadata(
            repo="test",
            generated_at="2026-01-01T00:00:00Z",
            total_files=2,
            languages=["python"],
        )
    )
    graph.nodes.append(
        Node(
            id="main.py",
            label="main.py",
            description="",
            symbols=[Symbol(name="run", kind="function", line=1)],
            language="python",
            size=20,
        )
    )
    graph.nodes.append(
        Node(
            id="utils.py",
            label="utils.py",
            description="",
            symbols=[Symbol(name="helper", kind="function", line=1)],
            language="python",
            size=30,
        )
    )
    return graph, tmp_path


# ---------------------------------------------------------------------------
# Client configuration
# ---------------------------------------------------------------------------


class TestClientConfig:
    def test_default_anthropic_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("OLLAMA_PORT", raising=False)

        key, base = _resolve_client_config(api_key=None, api_base=None)

        assert key == "anthropic-key"
        assert base == ANTHROPIC_BASE

    def test_ollama_env_alone_does_not_change_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        monkeypatch.setenv("OLLAMA_HOST", "localhost")
        monkeypatch.setenv("OLLAMA_PORT", "11434")

        key, base = _resolve_client_config(api_key=None, api_base=None)

        assert key == "openai-key"
        assert base is None

    def test_ollama_default_endpoint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        monkeypatch.delenv("OLLAMA_HOST", raising=False)
        monkeypatch.delenv("OLLAMA_PORT", raising=False)

        key, base = _resolve_client_config(api_key=None, api_base=None, ollama=True)

        assert key == "ollama"
        assert base == "http://127.0.0.1:11434/v1"

    def test_ollama_custom_host_and_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OLLAMA_API_KEY", "local-key")

        key, base = _resolve_client_config(
            api_key=None,
            api_base=None,
            ollama=True,
            ollama_host="192.168.1.20",
            ollama_port=11435,
        )

        assert key == "local-key"
        assert base == "http://192.168.1.20:11435/v1"

    def test_non_ollama_api_base_is_respected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-key")

        key, base = _resolve_client_config(
            api_key=None,
            api_base="https://llm.example.test/v1",
        )

        assert key == "anthropic-key"
        assert base == "https://llm.example.test/v1"


# ---------------------------------------------------------------------------
# Cache functions
# ---------------------------------------------------------------------------


class TestCache:
    def test_init_cache(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", tmp_path / ".cache")
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", tmp_path / ".cache" / "cache.db")
        conn = _init_cache()
        assert (tmp_path / ".cache" / "cache.db").exists()
        conn.close()

    def test_set_and_get_cached(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "cache.db"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", tmp_path)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", db_path)
        conn = _init_cache()

        _set_cached(conn, "main.py", "abc123", "Entry point for the application.")
        result = _get_cached(conn, "main.py", "abc123")
        assert result == "Entry point for the application."
        conn.close()

    def test_get_cached_miss(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db_path = tmp_path / "cache.db"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", tmp_path)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", db_path)
        conn = _init_cache()

        result = _get_cached(conn, "nonexistent.py", "xyz")
        assert result is None
        conn.close()

    def test_cache_replaces_on_content_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "cache.db"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", tmp_path)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", db_path)
        conn = _init_cache()

        _set_cached(conn, "main.py", "hash1", "Old description.")
        _set_cached(conn, "main.py", "hash2", "New description.")

        assert _get_cached(conn, "main.py", "hash1") == "Old description."
        assert _get_cached(conn, "main.py", "hash2") == "New description."
        conn.close()


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_hash_returns_hex(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("hello")
        h = _content_hash(f)
        assert len(h) == 64  # SHA-256 hex

    def test_hash_empty_on_missing_file(self) -> None:
        h = _content_hash(Path("/nonexistent/file.py"))
        assert h == ""

    def test_hash_changes_with_content(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("version 1")
        h1 = _content_hash(f)
        f.write_text("version 2")
        h2 = _content_hash(f)
        assert h1 != h2


# ---------------------------------------------------------------------------
# File preview and prompt
# ---------------------------------------------------------------------------


class TestPromptGeneration:
    def test_file_preview(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("\n".join(f"line {i}" for i in range(100)))
        node = Node(id="test.py", label="test.py", language="python")
        preview = _file_preview(tmp_path, node, lines=10)
        assert preview.count("\n") == 9  # 10 lines, 9 newlines

    def test_file_preview_missing_file(self, tmp_path: Path) -> None:
        node = Node(id="missing.py", label="missing.py", language="python")
        preview = _file_preview(tmp_path, node)
        assert preview == ""

    def test_make_prompt_includes_symbols(self, tmp_path: Path) -> None:
        f = tmp_path / "main.py"
        f.write_text("def run(): pass\n")
        node = Node(
            id="main.py",
            label="main.py",
            language="python",
            symbols=[Symbol(name="run", kind="function", line=1)],
        )
        prompt = _make_prompt(node, tmp_path)
        assert "run" in prompt
        assert "main.py" in prompt
        assert "python" in prompt


# ---------------------------------------------------------------------------
# describe() integration
# ---------------------------------------------------------------------------


class TestDescribe:
    def test_skips_when_openai_not_installed(
        self, graph_with_nodes: tuple[Graph, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph, root = graph_with_nodes
        # Simulate openai not installed
        import builtins

        real_import = builtins.__import__

        def mock_import(name: str, *args, **kwargs):
            if name == "openai":
                raise ImportError("No module named 'openai'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)
        result = describe(graph, root)
        # Should return graph unchanged
        assert all(n.description == "" for n in result.nodes)

    def test_uses_cache_on_second_call(
        self, graph_with_nodes: tuple[Graph, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        graph, root = graph_with_nodes
        cache_dir = tmp_path / ".test_cache"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", cache_dir)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", cache_dir / "cache.db")

        # Pre-populate cache — cache key now includes the audience level so
        # a voice change can invalidate descriptions without wiping the DB.
        conn = _init_cache()
        h = _content_hash(root / "main.py")
        _set_cached(conn, "main.py:newbie", h, "Cached description.")
        h2 = _content_hash(root / "utils.py")
        _set_cached(conn, "utils.py:newbie", h2, "Cached utils description.")
        conn.close()

        # Mock OpenAI at the module level (lazy import in describe())
        mock_client = MagicMock()
        with patch("openai.OpenAI", return_value=mock_client):
            result = describe(graph, root)

        assert result.nodes[0].description == "Cached description."
        assert result.nodes[1].description == "Cached utils description."
        mock_client.chat.completions.create.assert_not_called()

    def test_skips_already_described_nodes(
        self, graph_with_nodes: tuple[Graph, Path]
    ) -> None:
        graph, root = graph_with_nodes
        graph.nodes[0].description = "Already described."
        graph.nodes[1].description = "Also described."

        # Should not attempt LLM since all nodes have descriptions
        result = describe(graph, root)
        assert result.nodes[0].description == "Already described."

    def test_llm_failure_continues_with_partial_graph(
        self, graph_with_nodes: tuple[Graph, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LLM errors must not crash the run — they're logged and skipped."""
        graph, root = graph_with_nodes
        cache_dir = tmp_path / ".test_cache"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", cache_dir)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", cache_dir / "cache.db")

        # Mock OpenAI client so the first node raises and the second succeeds
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Second worked."))]
        mock_client.chat.completions.create.side_effect = [
            RuntimeError("simulated network error"),
            mock_response,
        ]

        with patch("openai.OpenAI", return_value=mock_client):
            result = describe(graph, root)

        # First node failed → empty description, second succeeded
        assert result.nodes[0].description == ""
        assert result.nodes[1].description == "Second worked."
        # Both attempts were made
        assert mock_client.chat.completions.create.call_count == 2

    def test_force_bypasses_cache(
        self, graph_with_nodes: tuple[Graph, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """force=True must regenerate even when the cache has fresh entries."""
        graph, root = graph_with_nodes
        cache_dir = tmp_path / ".test_cache"
        monkeypatch.setattr("prefxplain.describer.CACHE_DIR", cache_dir)
        monkeypatch.setattr("prefxplain.describer.CACHE_DB", cache_dir / "cache.db")

        # Pre-populate cache with stale descriptions
        conn = _init_cache()
        h_main = _content_hash(root / "main.py")
        h_utils = _content_hash(root / "utils.py")
        _set_cached(conn, "main.py", h_main, "Stale main.")
        _set_cached(conn, "utils.py", h_utils, "Stale utils.")
        conn.close()

        # Mock LLM to return fresh descriptions
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.choices = [MagicMock(message=MagicMock(content="Fresh!"))]
        mock_client.chat.completions.create.return_value = mock_response

        with patch("openai.OpenAI", return_value=mock_client):
            result = describe(graph, root, force=True)

        # All descriptions should be fresh, not cached
        assert result.nodes[0].description == "Fresh!"
        assert result.nodes[1].description == "Fresh!"
        # LLM was called for every node despite cache hits
        assert mock_client.chat.completions.create.call_count == 2
