import * as vscode from 'vscode';
import { randomBytes } from 'crypto';
import { readLaunchConfig } from '../config';
import { HostToWebview } from '../shared/panelProtocol';
import { SidecarClient, sidecarArgs, sidecarEnv } from '../sidecar';
import { PanelSession } from './panelSession';

/** The panel's view id (package.json `contributes.views`). */
export const VIEW_ID = 'langstage.chat';

/**
 * The LangStage panel (ADR 0001): a webview view in the activity bar whose React UI
 * talks to the extension host over postMessage, and the host to the panel's own
 * stdio sidecar. Only stable API is used (`registerWebviewViewProvider`), so it works in
 * every VS Code-based editor, with or without `vscode.chat`.
 */
export class PanelController implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;
  private readonly session: PanelSession;
  private readonly subs: vscode.Disposable[] = [];

  constructor(private readonly extensionUri: vscode.Uri) {
    this.session = new PanelSession({
      post: (msg) => this.post(msg),
      createClient: ({ demo }) => {
        const { python, agentSpec, workspace } = readLaunchConfig();
        return new SidecarClient({
          python,
          args: sidecarArgs(workspace, agentSpec, demo ? 'tools' : undefined),
          cwd: workspace,
          env: sidecarEnv(workspace),
        });
      },
      agentSpec: () => readLaunchConfig().agentSpec,
      openExternal: (url) => void this.confirmOpen(url),
      openSettings: () =>
        void vscode.commands.executeCommand('workbench.action.openSettings', 'langstage.'),
      copy: (text) => void vscode.env.clipboard.writeText(text),
    });
  }

  static register(context: vscode.ExtensionContext): PanelController {
    const controller = new PanelController(context.extensionUri);
    context.subscriptions.push(
      controller,
      vscode.window.registerWebviewViewProvider(VIEW_ID, controller, {
        // The host is the source of truth (`restore`), but keeping the webview alive
        // while hidden avoids a re-render for the common case.
        webviewOptions: { retainContextWhenHidden: true },
      }),
    );
    return controller;
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const webviewRoot = vscode.Uri.joinPath(this.extensionUri, 'dist', 'webview');
    view.webview.options = { enableScripts: true, localResourceRoots: [webviewRoot] };
    view.webview.html = renderHtml(view.webview, webviewRoot);
    this.subs.push(
      view.webview.onDidReceiveMessage((msg: unknown) => this.session.handle(msg)),
      view.onDidDispose(() => {
        if (this.view === view) this.view = undefined;
      }),
    );
    this.session.warmUp();
  }

  newConversation(): void {
    this.session.newConversation();
  }

  restartSidecar(): void {
    this.session.restart();
  }

  dispose(): void {
    this.session.dispose();
    for (const s of this.subs) s.dispose();
  }

  private post(msg: HostToWebview): void {
    void this.view?.webview.postMessage(msg);
  }

  /** Links in agent output are untrusted: confirm before opening (ADR 0001, Security). */
  private async confirmOpen(url: string): Promise<void> {
    const choice = await vscode.window.showInformationMessage(
      `Open this link from the agent's reply?\n\n${url}`,
      { modal: true },
      'Open',
    );
    if (choice === 'Open') await vscode.env.openExternal(vscode.Uri.parse(url));
  }
}

function renderHtml(webview: vscode.Webview, root: vscode.Uri): string {
  const nonce = randomBytes(16).toString('base64');
  const script = webview.asWebviewUri(vscode.Uri.joinPath(root, 'main.js'));
  const style = webview.asWebviewUri(vscode.Uri.joinPath(root, 'main.css'));
  const csp = [
    "default-src 'none'",
    `script-src 'nonce-${nonce}'`,
    `style-src ${webview.cspSource} 'unsafe-inline'`,
    `img-src ${webview.cspSource} data:`,
    `font-src ${webview.cspSource}`,
  ].join('; ');
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="${style}">
<title>LangStage</title>
</head>
<body>
<div id="root"></div>
<script nonce="${nonce}" src="${script}"></script>
</body>
</html>`;
}
