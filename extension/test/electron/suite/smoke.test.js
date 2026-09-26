// The LangStage extension in a real VS Code (build plan M5, real-editor layer):
// - it activates where the editor has no chat API (Cursor, VSCodium, code-server);
// - the panel's webview view resolves;
// - a `--demo=tools` turn round-trips through the real host (PanelController →
//   PanelSession → SidecarClient → python -m langstage_vscode).
/* global describe, it */
'use strict';
const assert = require('node:assert/strict');
const Module = require('node:module');
const vscode = require('vscode');

const EXTENSION_ID = 'dkedar7.langstage-vscode';

async function waitFor(pred, ms, what) {
  const until = Date.now() + ms;
  while (Date.now() < until) {
    if (pred()) return;
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`timed out waiting for ${what}`);
}

describe('LangStage in VS Code', () => {
  /** @type {{ panel: any }} */
  let api;

  it('activates without vscode.chat', async () => {
    const ext = vscode.extensions.getExtension(EXTENSION_ID);
    assert.ok(ext, 'the extension is installed');
    assert.equal(ext.isActive, false, 'nothing activated it before this test');

    // Hand the extension a `vscode` module with no `chat` namespace, as Cursor,
    // VSCodium and code-server do. The editor resolves `require('vscode')` through
    // Module._load; wrap it for requests from the extension's own files only.
    const root = ext.extensionPath.toLowerCase();
    const original = Module._load;
    let shimmed = 0;
    Module._load = function load(request, parent, isMain) {
      const loaded = original.call(this, request, parent, isMain);
      if (request === 'vscode' && parent && String(parent.filename).toLowerCase().startsWith(root)) {
        shimmed++;
        const withoutChat = { ...loaded };
        delete withoutChat.chat;
        return withoutChat;
      }
      return loaded;
    };
    try {
      api = await ext.activate();
    } finally {
      Module._load = original;
    }
    assert.ok(shimmed > 0, 'the extension loaded the vscode module without chat');
    assert.ok(api && api.panel, 'activate() returned');
    const commands = await vscode.commands.getCommands(true);
    for (const c of ['langstage.panel.focus', 'langstage.newConversation', 'langstage.restartSidecar']) {
      assert.ok(commands.includes(c), `${c} is registered`);
    }
  });

  it('opens the panel: the webview view resolves', async () => {
    await vscode.commands.executeCommand('langstage.panel.focus');
    await waitFor(() => api.panel.viewResolved, 30_000, 'the LangStage view to resolve');
  });

  it('round-trips a --demo=tools turn through the host, and a new conversation', async () => {
    const posted = [];
    const sub = api.panel.onDidPostForTests((m) => posted.push(m));
    try {
      api.panel.handleForTests({ v: 1, type: 'tryDemo' });
      await waitFor(
        () => posted.some((m) => m.type === 'status' && m.status.phase === 'ready'),
        90_000,
        'the demo sidecar to be ready',
      );
      api.panel.handleForTests({ v: 1, type: 'ui/ready' });
      const restore = posted.filter((m) => m.type === 'restore').pop();
      const id = restore.activeId;
      api.panel.handleForTests({ v: 1, type: 'send', conversationId: id, text: 'please use a tool' });
      await waitFor(
        () => posted.some((m) => m.type === 'turn/ended' && m.conversationId === id),
        60_000,
        'the turn to end',
      );
      const frames = posted.filter((m) => m.type === 'frame' && m.conversationId === id).map((m) => m.frame);
      const start = frames.find((f) => f.type === 'tool_start');
      const end = frames.find((f) => f.type === 'tool_end');
      assert.ok(start && end && start.id === end.id, 'a tool card');
      assert.equal(end.status, 'success');
      const reply = frames.filter((f) => f.type === 'content').map((f) => f.content).join('');
      assert.match(reply, /The demo tool returned/);

      await vscode.commands.executeCommand('langstage.newConversation');
      const after = posted.filter((m) => m.type === 'restore').pop();
      assert.notEqual(after.activeId, id, 'a new active conversation');
      assert.equal(after.conversations.length, 2, 'the first one is kept');
    } finally {
      sub.dispose();
    }
  });
});
