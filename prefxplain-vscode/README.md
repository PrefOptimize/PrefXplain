# PrefXplain Preview (VS Code Extension)

Opens `prefxplain.html` in a clean webview tab: no toolbar, no URL bar, just the diagram.
Auto-refreshes when the file changes (e.g., after `/prefxplain`).

## Install

```bash
cd prefxplain-vscode
npm install
npm run compile
npx @vscode/vsce package --allow-missing-repository
code --install-extension prefxplain-vscode-0.1.0.vsix
```

For Cursor: `cursor --install-extension prefxplain-vscode-0.1.0.vsix`
For Windsurf: `windsurf --install-extension prefxplain-vscode-0.1.0.vsix`

## Usage

### From Claude Code skills

`/prefxplain` triggers the extension automatically.

### From the command palette

`Cmd+Shift+P` > `PrefXplain: Preview diagram`

Opens `prefxplain.html` from the current workspace root.

`Cmd+Shift+P` > `PrefXplain: Generate diagram`

Runs `prefxplain create <workspace> --no-open` and opens the generated preview.
Generation options are read from VS Code settings.

## Settings

- `prefxplain.model` — value passed to `--model`
- `prefxplain.ollama.enabled` — enables `--ollama`
- `prefxplain.ollama.host` — value passed to `--ollama-host`
- `prefxplain.ollama.port` — value passed to `--ollama-port`

Example (`settings.json`):

```json
{
  "prefxplain.model": "llama3.1:8b",
  "prefxplain.ollama.enabled": true,
  "prefxplain.ollama.host": "127.0.0.1",
  "prefxplain.ollama.port": 11434
}
```

### From a URI (used by the skills)

```bash
open "vscode://prefxplain.prefxplain-vscode/preview?path=/absolute/path/to/prefxplain.html"
```

## Behavior

- **No chrome**: pure webview, the diagram fills the entire tab
- **Reuse**: calling the command again reveals the existing tab instead of opening a new one
- **Auto-refresh**: the preview reloads when `prefxplain.html` changes on disk, and also re-checks on focus/visibility so external slash-command runs stay in sync
- **Cleanup**: the watcher is disposed when the tab is closed

## Compatibility

Works with VS Code, Cursor, Windsurf, and VS Code Insiders.
