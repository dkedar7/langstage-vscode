// PanelSession (host side of the panel) against a fake sidecar: `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { PanelSession } from '../panel/panelSession';
import { HostToWebview } from '../shared/panelProtocol';
import { SidecarClient } from '../sidecar';
import { demoToolsTurns, fakeSpawn, tick } from './fakeSidecar';

function setup() {
  const { spawn, procs } = fakeSpawn();
  const posted: HostToWebview[] = [];
  const opened: string[] = [];
  const demos: boolean[] = [];
  const session = new PanelSession({
    post: (m) => posted.push(m),
    createClient: ({ demo }) => {
      demos.push(demo);
      return new SidecarClient({ python: 'python', args: [], cwd: '.', spawn, exitGraceMs: 20 });
    },
    agentSpec: () => '',
    openExternal: (url) => opened.push(url),
    openSettings: () => undefined,
    copy: () => undefined,
  });
  return { session, posted, procs, opened, demos };
}

function restoreOf(posted: HostToWebview[]) {
  const r = [...posted].reverse().find((m) => m.type === 'restore');
  assert.ok(r && r.type === 'restore');
  return r;
}

test('ui/ready starts the sidecar and restores one empty conversation', async () => {
  const { session, posted, procs } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  await tick();
  assert.equal(procs.length, 1);
  const r = restoreOf(posted);
  assert.equal(r.conversations.length, 1);
  assert.deepEqual(r.transcripts[r.activeId], []);
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  const last = posted[posted.length - 1];
  assert.equal(last.type, 'status');
  assert.equal(last.type === 'status' && last.status.phase, 'ready');
  session.dispose();
});

test('send → queued → started → frames forwarded verbatim → ended; the log coalesces', async () => {
  const { session, posted, procs } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }]);
  const id = restoreOf(posted).activeId;
  posted.length = 0;
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'please use a tool' });
  await tick();
  const cmd = procs[0].commands[0];
  assert.equal(cmd.type, 'message');
  assert.equal(cmd.content, 'please use a tool');
  assert.match(String(cmd.session_id), /^vscode-/);
  const [toolTurn] = demoToolsTurns();
  procs[0].emitFrames(toolTurn);
  await tick();
  await tick();
  const types = posted.map((m) => m.type);
  assert.deepEqual(types.slice(0, 3), ['user', 'turn/queued', 'turn/started']);
  assert.equal(types[types.length - 1], 'turn/ended');
  const frames = posted.flatMap((m) => (m.type === 'frame' ? [m.frame] : []));
  assert.deepEqual(frames, toolTurn);

  // Reload: the restore log replays the same transcript, with the token chunks joined.
  session.handle({ v: 1, type: 'ui/ready' });
  const log = restoreOf(posted).transcripts[id];
  const contents = log.filter((e) => e.kind === 'frame' && e.frame.type === 'content');
  assert.equal(contents.length, 1);
  assert.match(String(contents[0].kind === 'frame' && contents[0].frame.content), /^The demo tool returned/);
  assert.equal(restoreOf(posted).conversations[0].title, 'please use a tool');
  session.dispose();
});

test('a second send while the turn is in flight is ignored', async () => {
  const { session, posted, procs } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  const id = restoreOf(posted).activeId;
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'one' });
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'two' });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  assert.equal(procs[0].commands.length, 1);
  session.dispose();
});

test('cancel sends a cooperative cancel for the conversation session', async () => {
  const { session, posted, procs } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  const id = restoreOf(posted).activeId;
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'hello' });
  await tick();
  session.handle({ v: 1, type: 'cancel', conversationId: id });
  await tick();
  assert.deepEqual(procs[0].commands.map((c) => c.type), ['message', 'cancel']);
  assert.equal(procs[0].commands[1].session_id, procs[0].commands[0].session_id);
  session.dispose();
});

test('a startup failure shows in the status (noAgent) and the transcript; tryDemo restarts with --demo', async () => {
  const { session, posted, procs, demos } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  const id = restoreOf(posted).activeId;
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'hi' });
  await tick();
  procs[0].emitFrames([{ type: 'error', error: 'no agent spec (pass --agent or --demo)' }]);
  procs[0].exit(1);
  await tick();
  await tick();
  const status = [...posted].reverse().find((m) => m.type === 'status');
  assert.ok(status && status.type === 'status');
  assert.equal(status.status.phase, 'failed');
  assert.equal(status.status.noAgent, true);
  assert.match(status.status.error ?? '', /no agent spec/);
  const errFrame = posted.find((m) => m.type === 'frame' && m.frame.type === 'error');
  assert.ok(errFrame);
  assert.equal(posted[posted.length - 1].type, 'turn/ended');

  session.handle({ v: 1, type: 'tryDemo' });
  assert.deepEqual(demos, [false, true]);
  assert.equal(procs.length, 2);
  session.dispose();
});

test('conversation/new mints a fresh session id', async () => {
  const { session, posted, procs } = setup();
  session.handle({ v: 1, type: 'ui/ready' });
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  const first = restoreOf(posted).activeId;
  session.handle({ v: 1, type: 'conversation/new' });
  const r = restoreOf(posted);
  assert.notEqual(r.activeId, first);
  assert.equal(r.conversations.length, 1, 'the idle, unreachable conversation is dropped');
  session.handle({ v: 1, type: 'send', conversationId: first, text: 'to a dropped conversation' });
  await tick();
  assert.equal(procs[0].commands.length, 0);
  session.dispose();
});

test('malformed messages and unsafe links are dropped', () => {
  const { session, opened, procs } = setup();
  session.handle(null);
  session.handle({ type: 'send', conversationId: 'x', text: 'no version' });
  session.handle({ v: 1, type: 'send', conversationId: 1, text: 'bad id' });
  session.handle({ v: 1, type: 'openExternal', url: 'javascript:alert(1)' });
  session.handle({ v: 1, type: 'openExternal', url: 'command:workbench.action.openSettings' });
  session.handle({ v: 1, type: 'openExternal', url: 'https://example.com/x' });
  assert.deepEqual(opened, ['https://example.com/x']);
  assert.equal(procs.length, 0);
});
