import * as fs from "fs";
import * as path from "path";
import { spawn } from "child_process";
import * as vscode from "vscode";

let currentPanel: vscode.WebviewPanel | undefined;
let currentWatcher: vscode.FileSystemWatcher | undefined;
let currentHtmlPath: string | undefined;
let currentRefreshTimer: NodeJS.Timeout | undefined;
let currentPollTimer: NodeJS.Timeout | undefined;
let currentHtmlVersion: string | undefined;

function disposeWatcher(): void {
  if (currentWatcher) {
    currentWatcher.dispose();
    currentWatcher = undefined;
  }
}

function disposeRefreshLoop(): void {
  if (currentRefreshTimer) {
    clearTimeout(currentRefreshTimer);
    currentRefreshTimer = undefined;
  }
  if (currentPollTimer) {
    clearInterval(currentPollTimer);
    currentPollTimer = undefined;
  }
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return char;
    }
  });
}

function renderStatusHtml(title: string, message: string): string {
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>${escapeHtml(title)}</title>
  <style>
    body {
      margin: 0;
      padding: 24px;
      font-family: var(--vscode-font-family);
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
    }
    h1 { margin: 0 0 12px; font-size: 1.1rem; font-weight: 600; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word;
          font-family: var(--vscode-editor-font-family, var(--vscode-font-family)); }
  </style>
</head>
<body>
  <h1>${escapeHtml(title)}</h1>
  <pre>${escapeHtml(message)}</pre>
</body>
</html>`;
}

function injectBaseTag(rawHtml: string, baseHref: string): string {
  const baseTag = `<base href="${baseHref}">`;

  if (/<base\b[^>]*>/i.test(rawHtml)) {
    return rawHtml.replace(/<base\b[^>]*>/i, baseTag);
  }
  if (/<head\b[^>]*>/i.test(rawHtml)) {
    return rawHtml.replace(/<head\b[^>]*>/i, (match) => `${match}${baseTag}`);
  }
  if (/<html\b[^>]*>/i.test(rawHtml)) {
    return rawHtml.replace(
      /<html\b[^>]*>/i,
      (match) => `${match}<head>${baseTag}</head>`
    );
  }
  return `<head>${baseTag}</head>${rawHtml}`;
}

function injectVsCodeBridge(rawHtml: string): string {
  const bridgeScript = `<script>
(() => {
  try {
    if (
      typeof acquireVsCodeApi === 'function' &&
      (!window.__prefxplainVsCodeApi ||
        typeof window.__prefxplainVsCodeApi.postMessage !== 'function')
    ) {
      window.__prefxplainVsCodeApi = acquireVsCodeApi();
    }
  } catch (error) {
    console.warn('[prefxplain] unable to acquire VS Code API bridge', error);
  }
})();
</script>`;

  if (/<head\b[^>]*>/i.test(rawHtml)) {
    return rawHtml.replace(/<head\b[^>]*>/i, (match) => `${match}${bridgeScript}`);
  }
  if (/<html\b[^>]*>/i.test(rawHtml)) {
    return rawHtml.replace(
      /<html\b[^>]*>/i,
      (match) => `${match}<head>${bridgeScript}</head>`
    );
  }
  return `${bridgeScript}${rawHtml}`;
}

function injectResizeBridge(rawHtml: string): string {
  const bridgeScript = `<script>
(() => {
  const sync = () => {
    const root = document.documentElement;
    const rect = root.getBoundingClientRect();
    const width = Math.max(
      1,
      Math.round(rect.width || root.clientWidth || window.innerWidth || 1)
    );
    const height = Math.max(
      1,
      Math.round(rect.height || root.clientHeight || window.innerHeight || 1)
    );
    window.__prefxplainHostWidth = width;
    window.__prefxplainHostHeight = height;
    root.style.setProperty('--prefxplain-host-width', width + 'px');
    root.style.setProperty('--prefxplain-host-height', height + 'px');
    window.dispatchEvent(
      new CustomEvent('prefxplain-host-resize', { detail: { width, height } })
    );
  };

  let lastSize = '';
  const tick = () => {
    const root = document.documentElement;
    const next = [
      window.innerWidth || 0,
      window.innerHeight || 0,
      root.clientWidth || 0,
      root.clientHeight || 0,
    ].join('x');
    if (next !== lastSize) {
      lastSize = next;
      sync();
    }
    window.requestAnimationFrame(tick);
  };

  window.addEventListener('load', sync);
  window.addEventListener('resize', sync);
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', sync);
  }
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(sync).observe(document.documentElement);
  }
  sync();
  window.requestAnimationFrame(tick);
})();
</script>`;

  if (/<\/body>/i.test(rawHtml)) {
    return rawHtml.replace(/<\/body>/i, `${bridgeScript}</body>`);
  }
  return `${rawHtml}${bridgeScript}`;
}

function resolveSafeChildPath(rootDir: string, rel: string): string | null {
  if (!rel || typeof rel !== "string") return null;
  // Reject absolute paths and anything that escapes the workspace root.
  if (path.isAbsolute(rel)) return null;
  const candidate = path.resolve(rootDir, rel);
  const normalizedRoot = path.resolve(rootDir) + path.sep;
  if (!(candidate + path.sep).startsWith(normalizedRoot)) return null;
  return candidate;
}

function handleWebviewMessage(
  panel: vscode.WebviewPanel,
  htmlPath: string,
  msg: unknown
): void {
  if (!msg || typeof msg !== "object") return;
  const m = msg as { type?: string; id?: string; path?: string; content?: string };
  if (!m.type) return;

  // Reload messages don't need an id — they're fire-and-forget.
  // window.location.reload() blanks out VS Code webviews, so the UI posts
  // this instead and we re-set panel.webview.html on the host side.
  if (m.type === "prefxplain:reload") {
    refreshPreview(htmlPath, true);
    return;
  }

  if (!m.id) return;
  const reply = (payload: Record<string, unknown>): void => {
    panel.webview.postMessage({ id: m.id, ...payload });
  };
  const rootDir = path.dirname(htmlPath);

  if (m.type === "prefxplain:load-file") {
    const target = resolveSafeChildPath(rootDir, m.path || "");
    if (!target) { reply({ ok: false, error: "invalid path" }); return; }
    try {
      const content = fs.readFileSync(target, "utf-8");
      reply({ ok: true, content });
    } catch (error) {
      reply({ ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }

  if (m.type === "prefxplain:save-file") {
    const target = resolveSafeChildPath(rootDir, m.path || "");
    if (!target) { reply({ ok: false, error: "invalid path" }); return; }
    if (typeof m.content !== "string") { reply({ ok: false, error: "missing content" }); return; }
    try {
      fs.writeFileSync(target, m.content, "utf-8");
      reply({ ok: true });
    } catch (error) {
      reply({ ok: false, error: error instanceof Error ? error.message : String(error) });
    }
    return;
  }
}

function createPanel(htmlPath: string): vscode.WebviewPanel {
  const panel = vscode.window.createWebviewPanel(
    "prefxplain.preview",
    "PrefXplain",
    vscode.ViewColumn.Active,
    {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.file(path.dirname(htmlPath))],
    }
  );

  panel.onDidDispose(() => {
    currentPanel = undefined;
    currentHtmlPath = undefined;
    currentHtmlVersion = undefined;
    disposeWatcher();
    disposeRefreshLoop();
  });

  panel.onDidChangeViewState((event) => {
    if (!event.webviewPanel.visible || !currentHtmlPath) return;
    refreshPreview(currentHtmlPath);
  });

  panel.webview.onDidReceiveMessage((msg) => {
    if (!currentHtmlPath) return;
    handleWebviewMessage(panel, currentHtmlPath, msg);
  });

  return panel;
}

function loadHtml(panel: vscode.WebviewPanel, htmlPath: string): void {
  try {
    const raw = fs.readFileSync(htmlPath, "utf-8");
    const baseHref = `${panel.webview
      .asWebviewUri(vscode.Uri.file(path.dirname(htmlPath)))
      .toString()}/`;

    panel.webview.html = injectResizeBridge(
      injectVsCodeBridge(injectBaseTag(raw, baseHref))
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    panel.webview.html = renderStatusHtml(
      "PrefXplain",
      `Unable to load preview from:\n${htmlPath}\n\n${message}`
    );
  }
}

function showDeletedState(panel: vscode.WebviewPanel, htmlPath: string): void {
  panel.webview.html = renderStatusHtml(
    "PrefXplain",
    `File deleted:\n${htmlPath}\n\nWaiting for it to be recreated...`
  );
}

function getFileVersion(htmlPath: string): string | undefined {
  try {
    const stat = fs.statSync(htmlPath);
    return `${stat.mtimeMs}:${stat.size}`;
  } catch {
    return undefined;
  }
}

function refreshPreview(htmlPath: string, force = false): void {
  if (!currentPanel) return;
  if (!fs.existsSync(htmlPath)) {
    currentHtmlVersion = undefined;
    showDeletedState(currentPanel, htmlPath);
    return;
  }

  const nextVersion = getFileVersion(htmlPath);
  if (!force && nextVersion && currentHtmlVersion === nextVersion) {
    return;
  }

  loadHtml(currentPanel, htmlPath);
  currentHtmlVersion = nextVersion;
}

function scheduleRefresh(htmlPath: string, force = false): void {
  if (currentRefreshTimer) {
    clearTimeout(currentRefreshTimer);
  }
  currentRefreshTimer = setTimeout(() => {
    currentRefreshTimer = undefined;
    refreshPreview(htmlPath, force);
  }, 80);
}

function startRefreshLoop(htmlPath: string): void {
  disposeRefreshLoop();
  currentPollTimer = setInterval(() => {
    if (!currentPanel || currentHtmlPath !== htmlPath) return;
    refreshPreview(htmlPath);
  }, 750);
}

function setupWatcher(htmlPath: string): void {
  disposeWatcher();

  const pattern = new vscode.RelativePattern(
    path.dirname(htmlPath),
    path.basename(htmlPath)
  );

  currentWatcher = vscode.workspace.createFileSystemWatcher(pattern);

  const reload = (): void => {
    if (!currentPanel) return;
    scheduleRefresh(htmlPath, true);
  };

  currentWatcher.onDidChange(reload);
  currentWatcher.onDidCreate(reload);
  currentWatcher.onDidDelete(() => {
    if (currentPanel) {
      currentHtmlVersion = undefined;
      showDeletedState(currentPanel, htmlPath);
    }
  });

  startRefreshLoop(htmlPath);
}

/**
 * Resolve prefxplain.html when the URI or default path is wrong (common case:
 * workspace opened on `repo/prefxplain` while HTML is at `repo/prefxplain.html`).
 */
async function resolvePrefxplainHtmlPath(
  requested: string
): Promise<string | undefined> {
  const normalizedPath = path.resolve(requested);
  if (fs.existsSync(normalizedPath)) {
    return normalizedPath;
  }

  if (path.basename(normalizedPath) !== "prefxplain.html") {
    return undefined;
  }

  // .../repo/prefxplain/prefxplain.html -> .../repo/prefxplain.html
  const grandparent = path.dirname(path.dirname(normalizedPath));
  const oneLevelUp = path.join(grandparent, "prefxplain.html");
  if (fs.existsSync(oneLevelUp)) {
    return oneLevelUp;
  }

  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const atRoot = path.join(folder.uri.fsPath, "prefxplain.html");
    if (fs.existsSync(atRoot)) {
      return atRoot;
    }
  }

  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    const parent = path.join(path.dirname(folder.uri.fsPath), "prefxplain.html");
    if (fs.existsSync(parent)) {
      return parent;
    }
  }

  try {
    const found = await vscode.workspace.findFiles(
      "**/prefxplain.html",
      "**/{node_modules,.git,.venv,venv,dist,target,build}/**",
      5
    );
    if (found.length > 0) {
      found.sort((a, b) => a.fsPath.length - b.fsPath.length);
      return found[0].fsPath;
    }
  } catch {
    // ignore search errors
  }

  return undefined;
}

async function openPreview(htmlPath: string): Promise<void> {
  const resolved = await resolvePrefxplainHtmlPath(htmlPath);
  const normalizedPath = resolved ?? path.resolve(htmlPath);

  if (!fs.existsSync(normalizedPath)) {
    vscode.window.showErrorMessage(
      `PrefXplain: file not found: ${path.resolve(htmlPath)}. Run prefxplain from the repository root so it writes prefxplain.html there, or open the repo folder (not only the prefxplain/ package) in the IDE.`
    );
    return;
  }

  const nextDir = path.dirname(normalizedPath);
  const currentDir = currentHtmlPath
    ? path.dirname(currentHtmlPath)
    : undefined;

  // If directory changed, recreate panel (localResourceRoots needs updating)
  if (!currentPanel || currentDir !== nextDir) {
    if (currentPanel) currentPanel.dispose();
    currentPanel = createPanel(normalizedPath);
  } else {
    currentPanel.reveal(vscode.ViewColumn.Active);
  }

  currentHtmlPath = normalizedPath;
  currentHtmlVersion = getFileVersion(normalizedPath);
  loadHtml(currentPanel, normalizedPath);
  setupWatcher(normalizedPath);
}

function toHtmlPath(input?: string | vscode.Uri): string | undefined {
  if (!input) return undefined;
  return typeof input === "string" ? input : input.fsPath;
}

type GenerateSettings = {
  model: string;
  ollamaEnabled: boolean;
  ollamaHost: string;
  ollamaPort: number;
};

function getGenerateSettings(): GenerateSettings {
  const config = vscode.workspace.getConfiguration("prefxplain");
  return {
    model: String(config.get<string>("model") || "").trim(),
    ollamaEnabled: Boolean(config.get<boolean>("ollama.enabled", false)),
    ollamaHost: String(config.get<string>("ollama.host") || "").trim(),
    ollamaPort: Number(config.get<number>("ollama.port", 11434)),
  };
}

function buildGenerateArgs(workspaceRoot: string, settings: GenerateSettings): string[] {
  const args = ["create", workspaceRoot, "--no-open"];
  if (settings.model) {
    args.push("--model", settings.model);
  }
  if (settings.ollamaEnabled) {
    args.push("--ollama");
    if (settings.ollamaHost) {
      args.push("--ollama-host", settings.ollamaHost);
    }
    if (Number.isInteger(settings.ollamaPort) && settings.ollamaPort > 0) {
      args.push("--ollama-port", String(settings.ollamaPort));
    }
  }
  return args;
}

type GenerateResult = {
  code: number;
  stdout: string;
  stderr: string;
  error?: Error;
};

function runPrefxplainGenerate(workspaceRoot: string, args: string[]): Promise<GenerateResult> {
  return new Promise((resolve) => {
    const child = spawn("prefxplain", args, {
      cwd: workspaceRoot,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });

    let stdout = "";
    let stderr = "";

    child.stdout?.on("data", (chunk: Buffer | string) => {
      stdout += chunk.toString();
    });
    child.stderr?.on("data", (chunk: Buffer | string) => {
      stderr += chunk.toString();
    });

    child.on("error", (error) => {
      resolve({ code: -1, stdout, stderr, error });
    });
    child.on("close", (code) => {
      resolve({ code: code ?? -1, stdout, stderr });
    });
  });
}

function compactProcessOutput(stdout: string, stderr: string): string {
  const combined = `${stdout}\n${stderr}`.trim();
  if (!combined) return "";
  const lines = combined.split(/\r?\n/);
  return lines.slice(Math.max(0, lines.length - 12)).join("\n");
}

export function activate(context: vscode.ExtensionContext): void {
  // Cleanup on deactivation
  context.subscriptions.push({
    dispose: () => {
      disposeWatcher();
      disposeRefreshLoop();
      if (currentPanel) currentPanel.dispose();
    },
  });

  context.subscriptions.push(
    vscode.window.onDidChangeWindowState((state) => {
      if (!state.focused || !currentHtmlPath) return;
      refreshPreview(currentHtmlPath);
    })
  );

  // Command: prefxplain.preview
  context.subscriptions.push(
    vscode.commands.registerCommand(
      "prefxplain.preview",
      async (input?: string | vscode.Uri) => {
        let htmlPath = toHtmlPath(input);

        if (!htmlPath) {
          const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
          if (workspaceFolder) {
            htmlPath = path.join(workspaceFolder.uri.fsPath, "prefxplain.html");
          }
        }

        if (!htmlPath) {
          vscode.window.showErrorMessage(
            "PrefXplain: no path provided and no workspace folder open."
          );
          return;
        }

        await openPreview(htmlPath);
      }
    )
  );

  context.subscriptions.push(
    vscode.commands.registerCommand("prefxplain.generate", async () => {
      const workspaceFolder = vscode.workspace.workspaceFolders?.[0];
      if (!workspaceFolder) {
        vscode.window.showErrorMessage(
          "PrefXplain: open a workspace folder to generate a diagram."
        );
        return;
      }

      const workspaceRoot = workspaceFolder.uri.fsPath;
      const args = buildGenerateArgs(workspaceRoot, getGenerateSettings());
      const result = await vscode.window.withProgress(
        {
          location: vscode.ProgressLocation.Notification,
          title: "PrefXplain: generating diagram...",
          cancellable: false,
        },
        async () => runPrefxplainGenerate(workspaceRoot, args)
      );

      if (result.error) {
        const message = result.error.message || String(result.error);
        vscode.window.showErrorMessage(
          `PrefXplain: failed to start CLI (${message}). Ensure 'prefxplain' is on PATH.`
        );
        return;
      }

      if (result.code !== 0) {
        const tail = compactProcessOutput(result.stdout, result.stderr);
        const detail = tail ? `\n\n${tail}` : "";
        vscode.window.showErrorMessage(
          `PrefXplain: generation failed (exit ${result.code}).${detail}`
        );
        return;
      }

      await openPreview(path.join(workspaceRoot, "prefxplain.html"));
      vscode.window.showInformationMessage("PrefXplain: diagram generated.");
    })
  );

  // URI handler: vscode://prefxplain.prefxplain-vscode/preview?path=...
  context.subscriptions.push(
    vscode.window.registerUriHandler({
      async handleUri(uri: vscode.Uri): Promise<void> {
        const params = new URLSearchParams(uri.query);
        const htmlPath = params.get("path");

        if (!htmlPath) {
          vscode.window.showErrorMessage(
            'PrefXplain: missing "path" query parameter in URI.'
          );
          return;
        }

        await openPreview(htmlPath);
      },
    })
  );
}
