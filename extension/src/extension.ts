import * as vscode from 'vscode';
import { affectsLaunch } from './config';
import { PanelController, VIEW_ID } from './panel/PanelController';
import { ANSWER_COMMAND, disposeParticipantSidecar, handler } from './participant';

/**
 * LangStage for VS Code-based editors (ADR 0001). Two front ends over the same sidecar
 * protocol, each with its own sidecar process:
 *
 * - the **LangStage panel**, a webview view that needs nothing but the editor;
 * - the **`@langstage` chat participant**, for Copilot users. It is registered only
 *   where the editor provides the chat API: Cursor, VSCodium and code-server either
 *   lack `vscode.chat` or put their own assistant there, and calling it unguarded used
 *   to throw and fail activation (the panel must still work there).
 *
 * The extension declares `untrustedWorkspaces: { supported: false }`: running the
 * configured agent executes workspace code, so it stays disabled in Restricted Mode.
 */
export function activate(context: vscode.ExtensionContext) {
  const panel = PanelController.register(context);

  context.subscriptions.push(
    vscode.commands.registerCommand('langstage.panel.focus', () =>
      vscode.commands.executeCommand(`${VIEW_ID}.focus`),
    ),
    vscode.commands.registerCommand('langstage.newConversation', async () => {
      await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
      panel.newConversation();
    }),
    vscode.commands.registerCommand('langstage.restartSidecar', () => panel.restartSidecar()),
  );

  if (hasChatApi()) {
    const participant = vscode.chat.createChatParticipant('langstage.agent', handler);
    participant.iconPath = new vscode.ThemeIcon('robot');
    context.subscriptions.push(participant);
  }

  // The interrupt card's buttons: submit (or prefill) `@langstage /<verb>` in the chat,
  // so the answer runs as a turn of the same conversation and streams its reply there.
  context.subscriptions.push(
    vscode.commands.registerCommand(
      ANSWER_COMMAND,
      (q: { query: string; isPartialQuery: boolean }) =>
        vscode.commands.executeCommand('workbench.action.chat.open', q),
    ),
  );

  // Tear the participant's sidecar down when the extension unloads (the panel's goes
  // with `panel.dispose()` through context.subscriptions).
  context.subscriptions.push({ dispose: () => disposeParticipantSidecar() });

  // A changed interpreter or agent spec must not keep serving from a stale long-lived
  // process: the participant respawns on its next turn, the panel restarts now.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (affectsLaunch(e)) {
        disposeParticipantSidecar();
        panel.restartSidecar();
      }
    }),
  );
}

export function deactivate() {
  disposeParticipantSidecar();
}

/** Feature-detect the chat API instead of assuming it (Cursor, VSCodium, code-server). */
function hasChatApi(): boolean {
  try {
    const chat = (vscode as { chat?: { createChatParticipant?: unknown } }).chat;
    return typeof chat?.createChatParticipant === 'function';
  } catch {
    return false;
  }
}
