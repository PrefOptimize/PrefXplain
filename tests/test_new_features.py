"""Tests for v0.2.0 features: formats, filter, depth, check-cycles, matrix, roles, metrics."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from prefxplain.cli import app
from prefxplain.graph import Edge, Graph, GraphMetadata, Node
from prefxplain.renderer import render, render_matrix

runner = CliRunner()


def generated_artifact(project: Path, ext: str = ".html") -> Path:
    marker = project / ".prefxplain" / "latest"
    assert marker.exists()
    version = marker.read_text(encoding="utf-8").strip()
    assert version
    return project / ".prefxplain" / version / f"prefxplain{ext}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("from utils import helper\ndef run(): pass\n")
    (tmp_path / "utils.py").write_text("def helper(): return 42\n")
    return tmp_path


@pytest.fixture
def cyclic_project(tmp_path: Path) -> Path:
    (tmp_path / "a.py").write_text("from b import y\nx = 1\n")
    (tmp_path / "b.py").write_text("from a import x\ny = 2\n")
    return tmp_path


@pytest.fixture
def multi_project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "main.py").write_text("from src.utils import helper\ndef run(): pass\n")
    (src / "utils.py").write_text("def helper(): return 42\n")
    (tmp_path / "config.py").write_text("DEBUG = True\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Format options
# ---------------------------------------------------------------------------


class TestFormatOptions:
    def test_mermaid_format(self, py_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(py_project), "--no-descriptions", "--no-open", "--format", "mermaid"]
        )
        assert result.exit_code == 0, result.output
        md = generated_artifact(py_project, ".md")
        assert md.exists()
        content = md.read_text()
        assert "```mermaid" in content
        assert "graph LR" in content

    def test_dot_format(self, py_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(py_project), "--no-descriptions", "--no-open", "--format", "dot"]
        )
        assert result.exit_code == 0, result.output
        dot_file = generated_artifact(py_project, ".dot")
        assert dot_file.exists()
        content = dot_file.read_text()
        assert "digraph" in content

    def test_matrix_format(self, py_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(py_project), "--no-descriptions", "--no-open", "--format", "matrix"]
        )
        assert result.exit_code == 0, result.output
        html = generated_artifact(py_project, ".html")
        assert html.exists()
        content = html.read_text()
        assert "Matrix" in content


# ---------------------------------------------------------------------------
# Filter and depth
# ---------------------------------------------------------------------------


class TestFilterAndDepth:
    def test_filter_flag(self, multi_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(multi_project), "--no-descriptions", "--no-open", "--filter", "src/*"]
        )
        assert result.exit_code == 0, result.output
        assert "Filtered" in result.output

    def test_depth_with_focus(self, py_project: Path) -> None:
        result = runner.invoke(
            app, [
                "create", str(py_project), "--no-descriptions", "--no-open",
                "--focus", "main.py", "--depth", "0",
            ]
        )
        assert result.exit_code == 0, result.output
        assert "Focused" in result.output


# ---------------------------------------------------------------------------
# Check cycles
# ---------------------------------------------------------------------------


class TestCheckCycles:
    def test_check_cycles_fails_on_cycle(self, cyclic_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(cyclic_project), "--no-descriptions", "--no-open", "--check-cycles"]
        )
        assert result.exit_code == 1
        assert "FAIL" in result.output or "circular" in result.output.lower()

    def test_check_cycles_passes_no_cycle(self, py_project: Path) -> None:
        result = runner.invoke(
            app, ["create", str(py_project), "--no-descriptions", "--no-open", "--check-cycles"]
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Check command
# ---------------------------------------------------------------------------


class TestCheckCommand:
    def test_check_no_config(self, py_project: Path) -> None:
        result = runner.invoke(app, ["check", str(py_project)])
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_check_with_config(self, cyclic_project: Path) -> None:
        config = cyclic_project / ".prefxplain.yml"
        config.write_text("rules:\n  - name: no-circular-deps\n")
        result = runner.invoke(app, ["check", str(cyclic_project)])
        assert result.exit_code == 1
        assert "Circular" in result.output

    def test_check_passes_clean(self, py_project: Path) -> None:
        config = py_project / ".prefxplain.yml"
        config.write_text("rules:\n  - name: no-circular-deps\n")
        result = runner.invoke(app, ["check", str(py_project)])
        assert result.exit_code == 0
        assert "No violations" in result.output


# ---------------------------------------------------------------------------
# Matrix renderer
# ---------------------------------------------------------------------------


class TestMatrixRenderer:
    def test_matrix_basic(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py", language="python"),
                Node(id="b.py", label="b.py", language="python"),
            ],
            edges=[Edge(source="a.py", target="b.py")],
            metadata=GraphMetadata(repo="test", generated_at="2026-01-01", total_files=2, languages=["python"]),
        )
        html = render_matrix(g)
        assert "<table>" in html
        assert "a.py" in html
        assert "b.py" in html

    def test_matrix_cycle_highlighting(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py", language="python"),
                Node(id="b.py", label="b.py", language="python"),
            ],
            edges=[
                Edge(source="a.py", target="b.py"),
                Edge(source="b.py", target="a.py"),
            ],
            metadata=GraphMetadata(repo="test", generated_at="2026-01-01", total_files=2, languages=["python"]),
        )
        html = render_matrix(g)
        assert "cycle" in html

    def test_matrix_writes_file(self, tmp_path: Path) -> None:
        g = Graph(
            nodes=[Node(id="a.py", label="a.py")],
            edges=[],
            metadata=GraphMetadata(repo="test", generated_at="2026-01-01", total_files=1, languages=[]),
        )
        out = tmp_path / "matrix.html"
        render_matrix(g, output_path=out)
        assert out.exists()


# ---------------------------------------------------------------------------
# Role inference in renderer
# ---------------------------------------------------------------------------


class TestRolesInRenderer:
    def test_role_colors_in_html(self) -> None:
        g = Graph(
            nodes=[
                Node(id="main.py", label="main.py", language="python", role="entry_point"),
                Node(id="utils.py", label="utils.py", language="python", role="utility"),
            ],
            edges=[Edge(source="main.py", target="utils.py")],
            metadata=GraphMetadata(repo="test", generated_at="2026-01-01", total_files=2, languages=["python"]),
        )
        # These assertions are tied to the legacy Canvas renderer which
        # embeds ROLE_COLORS as a JS constant. The ELK renderer uses CSS
        # tokens on cards instead, so pin this check to the legacy path.
        html = render(g, renderer="legacy")
        assert "ROLE_COLORS" in html
        assert "entry_point" in html

    def test_cycle_info_in_html(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py", language="python"),
                Node(id="b.py", label="b.py", language="python"),
            ],
            edges=[
                Edge(source="a.py", target="b.py"),
                Edge(source="b.py", target="a.py"),
            ],
            metadata=GraphMetadata(repo="test", generated_at="2026-01-01", total_files=2, languages=["python"]),
        )
        # Legacy-only: the ELK renderer doesn't embed CYCLE_NODES / METRICS
        # as JS globals; cycles surface through edge state in the SVG.
        html = render(g, renderer="legacy")
        assert "CYCLE_NODES" in html
        assert "CYCLE_EDGES" in html
        assert "METRICS" in html


# ---------------------------------------------------------------------------
# Graph metrics + roles
# ---------------------------------------------------------------------------


class TestGraphMetrics:
    def test_metrics_keys(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py"),
                Node(id="b.py", label="b.py"),
            ],
            edges=[Edge(source="a.py", target="b.py")],
            metadata=GraphMetadata(repo="test", generated_at="", total_files=2, languages=["python"]),
        )
        m = g.metrics()
        assert "total_files" in m
        assert "total_edges" in m
        assert "cycles" in m
        assert "components" in m
        assert "top_centrality" in m
        assert "top_imported" in m
        assert "top_pagerank" in m

    def test_role_inference(self) -> None:
        g = Graph(
            nodes=[
                Node(id="main.py", label="main.py"),
                Node(id="src/utils.py", label="utils.py"),
                Node(id="config.py", label="config.py"),
                Node(id="tests/test_main.py", label="test_main.py"),
            ],
            edges=[],
        )
        g.infer_roles()
        assert g.get_node("main.py").role == "entry_point"
        assert g.get_node("src/utils.py").role == "utility"
        assert g.get_node("config.py").role == "config"
        assert g.get_node("tests/test_main.py").role == "test"

    def test_cluster_by_directory(self) -> None:
        g = Graph(
            nodes=[
                Node(id="src/a.py", label="a.py"),
                Node(id="src/b.py", label="b.py"),
                Node(id="lib/c.py", label="c.py"),
            ],
        )
        clusters = g.cluster_by_directory()
        assert "src" in clusters
        assert len(clusters["src"]) == 2
        assert "lib" in clusters

    def test_filter_subgraph(self) -> None:
        g = Graph(
            nodes=[
                Node(id="src/a.py", label="a.py"),
                Node(id="src/b.py", label="b.py"),
                Node(id="lib/c.py", label="c.py"),
            ],
            edges=[
                Edge(source="src/a.py", target="src/b.py"),
                Edge(source="src/a.py", target="lib/c.py"),
            ],
            metadata=GraphMetadata(repo="test", generated_at="", total_files=3, languages=["python"]),
        )
        sub = g.filter_subgraph("src/*")
        assert len(sub.nodes) == 2
        assert len(sub.edges) == 1  # only src/a -> src/b

    def test_depth_subgraph(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py"),
                Node(id="b.py", label="b.py"),
                Node(id="c.py", label="c.py"),
            ],
            edges=[
                Edge(source="a.py", target="b.py"),
                Edge(source="b.py", target="c.py"),
            ],
            metadata=GraphMetadata(repo="test", generated_at="", total_files=3, languages=["python"]),
        )
        sub = g.depth_subgraph("a.py", 1)
        ids = {n.id for n in sub.nodes}
        assert "a.py" in ids
        assert "b.py" in ids
        assert "c.py" not in ids  # 2 hops away

    def test_pagerank(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py"),
                Node(id="b.py", label="b.py"),
                Node(id="c.py", label="c.py"),
            ],
            edges=[
                Edge(source="a.py", target="c.py"),
                Edge(source="b.py", target="c.py"),
            ],
        )
        pr = g.pagerank()
        assert pr["c.py"] > pr["a.py"]  # c is most linked to

    def test_pagerank_preserves_probability_mass_with_sink_nodes(self) -> None:
        g = Graph(
            nodes=[
                Node(id="a.py", label="a.py"),
                Node(id="b.py", label="b.py"),
            ],
            edges=[Edge(source="a.py", target="b.py")],
        )
        pr = g.pagerank(iterations=20)
        assert sum(pr.values()) == pytest.approx(1.0, rel=1e-6)
        assert pr["b.py"] > pr["a.py"]
