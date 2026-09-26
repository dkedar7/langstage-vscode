// Scripted check of the panel's host side against the REAL sidecar, keyless:
//   python -m langstage_vscode --demo=tools
// It drives PanelSession (the host logic behind the webview) exactly as the webview
// would: ui/ready, then three `send`s, and asserts a streamed reply, a tool card and
// reasoning come back as the webview protocol's frames.
//
//   npm run compile
//   LANGSTAGE_PYTHON=../.venv/Scripts/python.exe node test/real-sidecar-check.js [--out restore.json]
//
// --out writes the final `restore` message (the host's transcript), for inspection.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { PanelSession } = require('../dist/panel/panelSession');
const { SidecarClient, sidecarArgs, sidecarEnv } = require('../dist/sidecar');

const envPython = process.env.LANGSTAGE_PYTHON || 'python';
// A path (not a bare command) is resolved here: the sidecar is spawned with cwd = workspace.
const python = /[\\/]/.test(envPython) ? path.resolve(envPython) : envPython;
const workspace = path.resolve(__dirname, '..', '..');
const outIdx = process.argv.indexOf('--out');
const out = outIdx > 0 ? process.argv[outIdx + 1] : undefined;

const posted = [];
let waiters = [];
const session = new PanelSession({
  post: (m) => {
    posted.push(m);
    waiters = waiters.filter((w) => !w(m));
  },
  createClient: ({ demo }) =>
    new SidecarClient({
      python,
      args: sidecarArgs(workspace, '', demo ? 'tools' : undefined),
      cwd: workspace,
      env: sidecarEnv(workspace),
    }),
  agentSpec: () => '',
  openExternal: () => undefined,
  openSettings: () => undefined,
  copy: () => undefined,
});

const waitFor = (pred, ms = 60000) =>
  new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('timed out')), ms);
    waiters.push((m) => (pred(m) ? (clearTimeout(t), resolve(m), true) : false));
  });

async function turn(conversationId, text) {
  const from = posted.length;
  session.handle({ v: 1, type: 'send', conversationId, text });
  await waitFor((m) => m.type === 'turn/ended' && m.conversationId === conversationId);
  return posted.slice(from);
}

(async () => {
  // No agent configured: the status reports it, and "Try the demo" recovers.
  session.handle({ v: 1, type: 'ui/ready' });
  const failed = await waitFor((m) => m.type === 'status' && m.status.phase !== 'starting');
  const restore0 = posted.find((m) => m.type === 'restore');
  const id = restore0.activeId;
  if (failed.status.phase === 'failed') {
    assert.equal(failed.status.noAgent, true, `expected the no-agent error, got: ${failed.status.error}`);
    console.log(`status: failed (noAgent) "${failed.status.error.slice(0, 60)}…"`);
    session.handle({ v: 1, type: 'tryDemo' });
  }
  const ready = await waitFor((m) => m.type === 'status' && m.status.phase === 'ready');
  assert.equal(ready.status.demo, true);
  console.log('status: ready (demo agent, --demo=tools)');

  // 1. A tool call: a tool card (tool_start → tool_end paired by id) and a streamed reply.
  let msgs = await turn(id, 'please use a tool');
  let frames = msgs.filter((m) => m.type === 'frame').map((m) => m.frame);
  const start = frames.find((f) => f.type === 'tool_start');
  const end = frames.find((f) => f.type === 'tool_end');
  assert.ok(start && end && start.id === end.id, 'tool card');
  assert.equal(end.status, 'success');
  const chunks = frames.filter((f) => f.type === 'content');
  assert.ok(chunks.length > 5, `streamed reply in ${chunks.length} chunks`);
  assert.deepEqual(
    msgs.slice(0, 3).map((m) => m.type),
    ['user', 'turn/queued', 'turn/started'],
  );
  console.log(
    `tool card: ${start.name}(${JSON.stringify(start.args)}) -> ${end.status} in ${end.duration_ms}ms; ` +
      `reply streamed in ${chunks.length} content frames: "${chunks.map((f) => f.content).join('').slice(0, 50)}…"`,
  );

  // 2. Reasoning, kept apart from the reply, then a reply with a NEW message_id.
  msgs = await turn(id, 'think about it');
  frames = msgs.filter((m) => m.type === 'frame').map((m) => m.frame);
  const reasoning = frames.filter((f) => f.type === 'reasoning');
  assert.ok(reasoning.length > 0, 'reasoning frames');
  const reply = frames.find((f) => f.type === 'content');
  assert.notEqual(reply.message_id, chunks[0].message_id, 'a new message_id per message');
  console.log(`reasoning: ${reasoning.length} frames: "${reasoning.map((f) => f.content).join('').slice(0, 50)}…"`);

  // 3. Stop once the turn has reached the sidecar: a cooperative `cancel`, after which
  // the same session keeps working (the demo is fast, so the turn may finish first).
  const from = posted.length;
  const started = waitFor((m) => m.type === 'turn/started' && m.conversationId === id);
  const ended = waitFor((m) => m.type === 'turn/ended' && m.conversationId === id);
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'hello there' });
  await started;
  session.handle({ v: 1, type: 'cancel', conversationId: id });
  await ended;
  const stopped = posted.slice(from).some((m) => m.type === 'frame' && m.frame.type === 'cancelled');
  console.log(`cancel: ${stopped ? 'sidecar answered cancelled -> turn_end' : 'the turn finished before the cancel landed'}`);
  msgs = await turn(id, 'still there?');
  assert.ok(msgs.some((m) => m.type === 'frame' && m.frame.type === 'content'), 'works after a cancel');
  console.log('after cancel: the next turn on the same session still replies');

  session.handle({ v: 1, type: 'ui/ready' });
  const restore = [...posted].reverse().find((m) => m.type === 'restore');
  const log = restore.transcripts[id];
  const frameCount = posted.filter((m) => m.type === 'frame').length;
  console.log(`restore: ${log.length} log entries for ${frameCount} live frames (coalesced)`);
  if (out) {
    fs.writeFileSync(out, JSON.stringify(restore, null, 2));
    console.log(`wrote ${out}`);
  }
  session.dispose();
  console.log('OK');
})().catch((err) => {
  console.error('FAILED:', err);
  session.dispose();
  process.exit(1);
});
