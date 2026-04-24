"""Tests for the CLI entry point."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from prefxplain import cli as cli_mod
from prefxplain.cli import _install_vscode_extension as real_install_vscode_extension
from prefxplain.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def py_project(tmp_path: Path) -> Path:
    """Minimal Python project for CLI tests."""
    (tmp_path / "main.py").write_text("from utils import helper\ndef run(): pass\n")
    (tmp_path / "utils.py").write_text("def helper(): return 42\n")
    return tmp_path


# ---------------------------------------------------------------------------
# Create command
# ---------------------------------------------------------------------------


class TestCreateCommand:
    def test_create_help_includes_ollama_flags(self) -> None:
        result = runner.invoke(app, ["create", "--help"])
        assert result.exit_code == 0, result.output
        assert "--ollama" in result.output
        assert "--ollama-host" in result.output
        assert "--ollama-port" in result.output

    def test_create_produces_html(self, py_project: Path) -> None:
        result = runner.invoke(app, ["create", str(py_project), "--no-descriptions", "--no-open"])
        assert result.exit_code == 0, result.output
        assert (py_project / "prefxplain.html").exists()
        assert (py_project / "prefxplain.json").exists()

    def test_create_custom_output(self, py_project: Path, tmp_path: Path) -> None:
        out = tmp_path / "custom.html"
        result = runner.invoke(
            app, ["create", str(py_project), "--no-descriptions", "--no-open", "-o", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.exists()

    def test_create_reports_file_count(self, py_project: Path) -> None:
        result = runner.invoke(app, ["create", str(py_project), "--no-descriptions", "--no-open"])
        assert result.exit_code == 0
        assert "2 files" in result.output

    def test_create_max_files(self, py_project: Path) -> None:
        result = runner.invoke(
            app,
            ["create", str(py_project), "--no-descriptions", "--no-open", "--max-files", "1"],
        )
        assert result.exit_code == 0
        assert "1 file" in result.output or "1 files" in result.output


# ---------------------------------------------------------------------------
# Update command
# ---------------------------------------------------------------------------


class TestUpdateCommand:
    def test_update_help_includes_ollama_flags(self) -> None:
        result = runner.invoke(app, ["update", "--help"])
        assert result.exit_code == 0, result.output
        assert "--ollama" in result.output
        assert "--ollama-host" in result.output
        assert "--ollama-port" in result.output

    def test_update_after_create(self, py_project: Path) -> None:
        # First create
        runner.invoke(app, ["create", str(py_project), "--no-descriptions", "--no-open"])
        assert (py_project / "prefxplain.json").exists()

        # Then update
        result = runner.invoke(app, ["update", str(py_project), "--no-descriptions", "--no-open"])
        assert result.exit_code == 0, result.output
        assert (py_project / "prefxplain.html").exists()


# ---------------------------------------------------------------------------
# Version flag
# ---------------------------------------------------------------------------


class TestVersion:
    def test_version_flag(self) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "prefxplain" in result.output


# ---------------------------------------------------------------------------
# Setup command
# ---------------------------------------------------------------------------


class TestSetupCommand:
    @pytest.fixture(autouse=True)
    def _stub_preview_extension_install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli_mod, "_install_vscode_extension", lambda _package_root: None)

    def test_install_vscode_extension_uses_packaged_vsix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        ext_dir = package_root / "vscode_extension"
        ext_dir.mkdir(parents=True)
        vsix = ext_dir / "prefxplain-vscode-0.1.0.vsix"
        vsix.write_text("stub", encoding="utf-8")

        real_exists = cli_mod.Path.exists

        def fake_exists(path_obj: Path) -> bool:
            if str(path_obj).startswith("/Applications/"):
                return False
            return real_exists(path_obj)

        monkeypatch.setattr(cli_mod.Path, "exists", fake_exists)
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda name: "/usr/bin/code" if name == "code" else None,
        )

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
            calls.append(cmd)
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(cli_mod.subprocess, "run", fake_run)

        result = real_install_vscode_extension(package_root)

        assert result == "Preview extension (VS Code): prefxplain-vscode-0.1.0.vsix"
        assert calls == [[
            "/usr/bin/code",
            "--install-extension",
            str(vsix),
            "--force",
        ]]

    def test_setup_succeeds_with_preview_extension_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)
        monkeypatch.setattr(
            cli_mod,
            "_install_vscode_extension",
            lambda _package_root: "Preview extension (VS Code): prefxplain-vscode-0.1.0.vsix",
        )

        result = runner.invoke(app, ["setup"])

        assert result.exit_code == 0, result.output
        assert "Preview extension (VS Code): prefxplain-vscode-0.1.0.vsix" in result.output
        assert "No AI coding tools detected, so /prefxplain was not registered yet." in result.output

    def test_setup_autodetect_skips_codex_and_prints_project_note(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        cmd_dir = package_root / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "prefxplain.md").write_text("prefxplain", encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda name: "/usr/bin/codex" if name == "codex" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0, result.output
        assert "Codex detected, but its setup is project-local." in result.output
        assert not (tmp_path / "AGENTS.md").exists()

    def test_setup_codex_installs_into_current_project(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        project_dir = tmp_path / "proj"
        project_dir.mkdir()

        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda name: "/usr/bin/codex" if name == "codex" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup", "codex"])
        assert result.exit_code == 0, result.output
        assert "Codex (project):" in result.output
        dest = project_dir / "AGENTS.md"
        assert dest.exists()
        assert "run `prefxplain .` to generate an interactive HTML dependency graph." in dest.read_text()

    def test_setup_copilot_installs_plugin(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        calls: list[list[str]] = []

        def fake_call(cmd: list[str], **_: object) -> int:
            calls.append(cmd)
            return 0

        monkeypatch.setattr(cli_mod.subprocess, "call", fake_call)

        result = runner.invoke(app, ["setup", "copilot"])
        assert result.exit_code == 0, result.output
        assert "Copilot CLI (global plugin):" in result.output
        assert calls == [[
            "/usr/bin/copilot",
            "plugin",
            "install",
            str(plugin_dir),
        ]]

    def test_setup_copilot_missing_cli_fails(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup", "copilot"])
        assert result.exit_code == 1
        assert "copilot CLI not found on PATH." in result.output

    def test_setup_autodetect_skips_copilot_when_non_interactive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Auto-detect must NOT silently install Copilot — the npm-backed
        plugin install can take 3-4 min, so we ask first. When stdin is not a
        TTY (LLM-driven `./setup` runs through the Bash tool), skip silently
        and tell the user how to opt in later.
        """
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")
        cmd_dir = package_root / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "prefxplain.md").write_text("prefxplain", encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda name: "/usr/bin/copilot" if name == "copilot" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        monkeypatch.setattr(cli_mod, "_stdin_is_interactive", lambda: False)

        # subprocess.call must NOT be invoked — we should bail before reaching it.
        def _should_not_be_called(*_a: object, **_kw: object) -> object:
            raise AssertionError("copilot plugin install must not run when prompt is skipped")

        monkeypatch.setattr(cli_mod.subprocess, "call", _should_not_be_called)

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0, result.output
        assert "Copilot CLI (global plugin):" not in result.output
        assert "non-interactive" in result.output.lower()

    def test_setup_autodetect_installs_copilot_when_user_confirms(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """When stdin is a TTY and the user answers 'y', auto-detect proceeds
        with the (slow) Copilot plugin install.
        """
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")
        cmd_dir = package_root / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "prefxplain.md").write_text("prefxplain", encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil,
            "which",
            lambda name: "/usr/bin/copilot" if name == "copilot" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)
        monkeypatch.setattr(cli_mod, "_stdin_is_interactive", lambda: True)

        monkeypatch.setattr(cli_mod.subprocess, "call", lambda *_a, **_kw: 0)

        result = runner.invoke(app, ["setup"], input="y\n")
        assert result.exit_code == 0, result.output
        assert "Copilot CLI (global plugin):" in result.output

    def test_setup_autodetect_ignores_copilot_home_without_binary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        cmd_dir = package_root / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "prefxplain.md").write_text("prefxplain", encoding="utf-8")

        # Home has .copilot, but no executable => should not auto-detect copilot.
        (tmp_path / ".copilot").mkdir(parents=True)
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 1
        assert "No AI coding tools detected." in result.output

    def test_setup_copilot_install_failure_exits_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        monkeypatch.setattr(cli_mod.subprocess, "call", lambda *_args, **_kwargs: 2)

        result = runner.invoke(app, ["setup", "copilot"])
        assert result.exit_code == 1
        assert "Failed to install Copilot plugin." in result.output

    def test_setup_copilot_install_cancelled_by_ctrl_c(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Ctrl-C during the (now-untimed) Copilot install must be a clean exit
        with a clear message, not a Python traceback. Replaces the obsolete
        timeout test — we deliberately removed the timeout because npm-backed
        plugin installs can legitimately take 10+ minutes on slow VMs.
        """
        package_root = tmp_path / "prefxplain"
        plugin_dir = package_root / "copilot_plugin"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.json").write_text('{"name":"prefxplain-copilot"}', encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda name: "/usr/bin/copilot" if name == "copilot" else None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        def _interrupt(*_args: object, **_kwargs: object):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli_mod.subprocess, "call", _interrupt)

        result = runner.invoke(app, ["setup", "copilot"])
        assert result.exit_code == 130
        assert "cancelled" in result.output.lower()

    # --- Gemini CLI ---

    def _make_gemini_fixture(self, tmp_path: Path):
        """Create a fake package layout with a valid SKILL.md and return the package_root."""
        package_root = tmp_path / "prefxplain"
        skill_dir = package_root / "copilot_plugin" / "skills" / "prefxplain"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: prefxplain\ndescription: test\n---\n\nbody\n",
            encoding="utf-8",
        )
        return package_root

    def test_setup_gemini_installs_skill_global(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = self._make_gemini_fixture(tmp_path)
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil, "which",
            lambda name: "/usr/bin/gemini" if name == "gemini" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup", "gemini"])
        assert result.exit_code == 0, result.output
        assert "Gemini CLI (global):" in result.output

        dest = tmp_path / ".gemini" / "skills" / "prefxplain" / "SKILL.md"
        assert dest.exists()
        assert "name: prefxplain" in dest.read_text()

    def test_setup_gemini_project_scope(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = self._make_gemini_fixture(tmp_path)
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil, "which",
            lambda name: "/usr/bin/gemini" if name == "gemini" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup", "gemini", "--project"])
        assert result.exit_code == 0, result.output
        assert "Gemini CLI (project):" in result.output

        dest = project_dir / ".gemini" / "skills" / "prefxplain" / "SKILL.md"
        assert dest.exists()

    def test_setup_autodetect_includes_gemini(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = self._make_gemini_fixture(tmp_path)
        cmd_dir = package_root / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "prefxplain.md").write_text("prefxplain", encoding="utf-8")

        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil, "which",
            lambda name: "/usr/bin/gemini" if name == "gemini" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0, result.output
        assert "Gemini CLI (global):" in result.output

    def test_setup_autodetect_includes_gemini_via_home_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        package_root = self._make_gemini_fixture(tmp_path)
        (tmp_path / ".gemini").mkdir()
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(cli_mod.shutil, "which", lambda _name: None)
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup"])
        assert result.exit_code == 0, result.output
        assert "Gemini CLI (global):" in result.output

    def test_setup_gemini_missing_skill_asset_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # No SKILL.md in the fake package.
        package_root = tmp_path / "prefxplain"
        package_root.mkdir()
        monkeypatch.setattr(cli_mod.Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(
            cli_mod.shutil, "which",
            lambda name: "/usr/bin/gemini" if name == "gemini" else None,
        )
        monkeypatch.setattr(cli_mod, "__file__", str(package_root / "cli.py"), raising=False)

        result = runner.invoke(app, ["setup", "gemini"])
        assert result.exit_code == 1
        assert "Agent skill asset missing" in result.output


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_directory(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["create", str(tmp_path), "--no-descriptions", "--no-open"])
        assert result.exit_code == 0
        assert "0 files" in result.output

    def test_html_is_self_contained(self, py_project: Path) -> None:
        runner.invoke(app, ["create", str(py_project), "--no-descriptions", "--no-open"])
        html = (py_project / "prefxplain.html").read_text()
        assert "<script>" in html
        assert "<style>" in html
        # No external CDN references
        assert "cdn." not in html.lower()

    def test_open_output_prefers_ide_preview_uri(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        html_path = tmp_path / "prefxplain.html"
        html_path.write_text("<html></html>")

        launched: list[list[str]] = []
        browser_urls: list[str] = []

        class _DummyProcess:
            pass

        def fake_popen(cmd: list[str], **_: object) -> _DummyProcess:
            launched.append(cmd)
            return _DummyProcess()

        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        monkeypatch.setattr(cli_mod.sys, "platform", "darwin", raising=False)
        monkeypatch.setattr(cli_mod.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(cli_mod.webbrowser, "open", browser_urls.append)

        cli_mod._open_output(html_path)

        assert launched
        assert launched[0][0] == "open"
        assert launched[0][1].startswith("vscode://prefxplain.prefxplain-vscode/preview?path=")
        assert not browser_urls
