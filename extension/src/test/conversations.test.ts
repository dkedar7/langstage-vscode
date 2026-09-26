// M4: conversations, persistence, queueing and memory honesty (PanelSession +
// ConversationStore against a fake sidecar): `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { ConversationStore } from '../panel/conversationStore';
import { PanelSession } from '../panel/panelSession';
import { HostToWebview, LogEntry } from '../shared/panelProtocol';
import { SidecarClient } from '../sidecar';
import { fakeSpawn, tick } from './fakeSidecar';

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'ls-store-'));
}

function setup(dir?: string) {
  const { spawn, procs } = fakeSpawn();
  const posted: HostToWebview[] = [];
  const store = new ConversationStore(dir, 5);
  const session = new PanelSession({
    post: (m) => posted.push(m),
    createClient: () => new SidecarClient({ python: 'python', args: [], cwd: '.', spawn, exitGraceMs: 20 }),
    agentSpec: () => '',
    openExternal: () => undefined,
    openSettings: () => undefined,
    copy: () => undefined,
    store,
  });
  return { session, posted, procs, store };
}

function restoreOf(posted: HostToWebview[]) {
  const r = [...posted].reverse().find((m) => m.type === 'restore');
  assert.ok(r && r.type === 'restore');
  return r;
}

async function ready(session: PanelSession, posted: HostToWebview[], procs: ReturnType<typeof fakeSpawn>['procs']) {
  session.handle({ v: 1, type: 'ui/ready' });
  await tick();
  procs[procs.length - 1].emitFrames([{ type: 'ready' }]);
  await tick();
  return restoreOf(posted).activeId;
}

const endTurn = (sid: unknown) => [{ type: 'turn_end', session_id: sid }];

test('the store round-trips conversations and skips corrupt or foreign files', () => {
  const dir = tmpDir();
  const store = new ConversationStore(dir, 5);
  const log: LogEntry[] = [
    { kind: 'user', text: 'hi' },
    { kind: 'frame', frame: { type: 'content', content: 'hello' } },
    { kind: 'decision', decisions: [{ type: 'approve' }] },
  ];
  const id = 'c-0e8f5a4e-1111-4222-8333-944455556666';
  store.bind(() => ({
    activeId: id,
    conversations: [{ id, title: 'Hi', sessionId: 'vscode-1', createdAt: 1, updatedAt: 2, log }],
  }));
  store.touch(id);
  store.flush();
  const back = new ConversationStore(dir).load();
  assert.equal(back.activeId, id);
  assert.deepEqual(back.conversations[0].log, log);
  assert.equal(back.conversations[0].sessionId, 'vscode-1');

  // A damaged transcript loads as empty; a bad id is never used as a file name.
  fs.writeFileSync(path.join(dir, 'transcripts', `${id}.json`), '{not json');
  const index = JSON.parse(fs.readFileSync(path.join(dir, 'conversations.json'), 'utf8'));
  index.conversations.push({ id: '../../evil', title: 'x', sessionId: 's' });
  fs.writeFileSync(path.join(dir, 'conversations.json'), JSON.stringify(index));
  const damaged = new ConversationStore(dir).load();
  assert.equal(damaged.conversations.length, 1);
  assert.deepEqual(damaged.conversations[0].log, []);

  fs.writeFileSync(path.join(dir, 'conversations.json'), 'garbage');
  assert.deepEqual(new ConversationStore(dir).load(), { conversations: [] });
  assert.equal(new ConversationStore(undefined).persistent, false);
});

test('conversations persist per workspace and come back with a memory_reset marker', async () => {
  const dir = tmpDir();
  const a = setup(dir);
  const id = await ready(a.session, a.posted, a.procs);
  a.session.handle({ v: 1, type: 'send', conversationId: id, text: 'remember 7\nplease' });
  await tick();
  const sid = a.procs[0].commands[0].session_id;
  a.procs[0].emitFrames([{ type: 'ack', ref: 'message' }, { type: 'content', content: 'ok' }, ...endTurn(sid)]);
  await tick();
  a.session.handle({ v: 1, type: 'conversation/rename', conversationId: id, title: '  Seven  ' });
  a.session.dispose(); // flushes

  // A window reload: a new host session over the same storage, a new sidecar.
  const b = setup(dir);
  const bid = await ready(b.session, b.posted, b.procs);
  assert.equal(bid, id, 'the active conversation comes back');
  const r = restoreOf(b.posted);
  assert.equal(r.persistent, true);
  assert.equal(r.conversations[0].title, 'Seven');
  const log = r.transcripts[id];
  assert.deepEqual(log[0], { kind: 'user', text: 'remember 7\nplease', at: (log[0] as { at: number }).at });
  const last = log[log.length - 1];
  assert.ok(last.kind === 'frame' && last.frame.type === 'memory_reset', 'restored memory is flagged');

  // It keeps its session id (a durable checkpointer would still have the thread).
  b.session.handle({ v: 1, type: 'send', conversationId: id, text: 'what number?' });
  await tick();
  assert.equal(b.procs[0].commands[0].session_id, sid);
  b.session.dispose();

  // Reloading again does not stack a second marker on an untouched transcript... but
  // the turn above added history, so exactly one more marker follows it.
  const c = setup(dir);
  await ready(c.session, c.posted, c.procs);
  const log2 = restoreOf(c.posted).transcripts[id];
  const markers = log2.filter((e) => e.kind === 'frame' && e.frame.type === 'memory_reset');
  assert.equal(markers.length, 2);
  c.session.dispose();
});

test('a restart of a sidecar that served turns marks memory reset; a fresh one does not', async () => {
  const { session, posted, procs } = setup();
  const id = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'restartSidecar' });
  assert.ok(!posted.some((m) => m.type === 'frame' && m.frame.type === 'memory_reset'), 'nothing to forget yet');
  procs[1].emitFrames([{ type: 'ready' }]);
  await tick();
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'hi' });
  await tick();
  procs[1].emitFrames([{ type: 'ack', ref: 'message' }, ...endTurn('x')]);
  await tick();
  session.handle({ v: 1, type: 'restartSidecar' });
  const resets = posted.filter((m) => m.type === 'frame' && m.frame.type === 'memory_reset');
  assert.equal(resets.length, 1);
  assert.equal(resets[0].type === 'frame' && resets[0].conversationId, id);
  session.dispose();
});

test('the sidecar dying on its own also marks memory reset', async () => {
  const { session, posted, procs } = setup();
  const id = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'hi' });
  await tick();
  procs[0].emitFrames([{ type: 'ack', ref: 'message' }, ...endTurn('x')]);
  await tick();
  procs[0].exit(1);
  await new Promise((r) => setTimeout(r, 40));
  assert.ok(posted.some((m) => m.type === 'frame' && m.frame.type === 'memory_reset'));
  session.dispose();
});

test('one turn at a time: B queues behind A; Stop in B cancels only B', async () => {
  const { session, posted, procs } = setup();
  const a = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'send', conversationId: a, text: 'first' });
  await tick();
  session.handle({ v: 1, type: 'conversation/new' });
  const b = restoreOf(posted).activeId;
  assert.notEqual(a, b);
  posted.length = 0;
  session.handle({ v: 1, type: 'send', conversationId: b, text: 'second' });
  await tick();
  assert.deepEqual(
    posted.map((m) => m.type),
    ['user', 'turn/queued'],
    'B waits (queued) while A runs',
  );
  assert.equal(procs[0].commands.length, 1, 'B has not reached the sidecar');
  // The list shows it: A running, B queued.
  session.handle({ v: 1, type: 'ui/ready' });
  assert.deepEqual(restoreOf(posted).turns, { [a]: 'running', [b]: 'queued' });

  session.handle({ v: 1, type: 'cancel', conversationId: b });
  await tick();
  await tick();
  assert.ok(posted.some((m) => m.type === 'frame' && m.conversationId === b && m.frame.type === 'cancelled'));
  assert.ok(posted.some((m) => m.type === 'turn/ended' && m.conversationId === b));
  assert.deepEqual(procs[0].commands.map((c) => c.type), ['message'], 'A was not cancelled');
  session.dispose();
});

test('queued B runs when A ends', async () => {
  const { session, posted, procs } = setup();
  const a = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'send', conversationId: a, text: 'first' });
  await tick();
  session.handle({ v: 1, type: 'conversation/new' });
  const b = restoreOf(posted).activeId;
  session.handle({ v: 1, type: 'send', conversationId: b, text: 'second' });
  await tick();
  procs[0].emitFrames(endTurn(procs[0].commands[0].session_id));
  await tick();
  await tick();
  assert.equal(procs[0].commands.length, 2);
  assert.equal(procs[0].commands[1].content, 'second');
  assert.ok(posted.some((m) => m.type === 'turn/started' && m.conversationId === b));
  session.dispose();
});

test('switch, rename and delete; delete refuses a conversation with a turn in flight', async () => {
  const dir = tmpDir();
  const { session, posted, procs } = setup(dir);
  const a = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'send', conversationId: a, text: 'alpha' });
  await tick();
  session.handle({ v: 1, type: 'conversation/new' });
  const b = restoreOf(posted).activeId;
  session.handle({ v: 1, type: 'conversation/delete', conversationId: a });
  assert.equal(restoreOf(posted).conversations.length, 2, 'A is still running');
  procs[0].emitFrames(endTurn('x'));
  await tick();
  session.handle({ v: 1, type: 'conversation/switch', conversationId: a });
  assert.equal(restoreOf(posted).activeId, a);
  session.handle({ v: 1, type: 'conversation/rename', conversationId: b, title: 'Beta' });
  assert.equal(restoreOf(posted).conversations.find((c) => c.id === b)?.title, 'Beta');
  session.handle({ v: 1, type: 'conversation/delete', conversationId: a });
  const r = restoreOf(posted);
  assert.deepEqual(r.conversations.map((c) => c.id), [b]);
  assert.equal(r.activeId, b);
  session.dispose();
  assert.ok(!fs.existsSync(path.join(dir, 'transcripts', `${a}.json`)), 'the transcript file is removed');
  // Deleting the last one leaves a fresh, empty conversation.
  const again = setup(dir);
  await ready(again.session, again.posted, again.procs);
  again.session.handle({ v: 1, type: 'conversation/delete', conversationId: b });
  const r2 = restoreOf(again.posted);
  assert.equal(r2.conversations.length, 1);
  assert.notEqual(r2.activeId, b);
  again.session.dispose();
});

test('decide logs the decision and sends it on the conversation session', async () => {
  const { session, posted, procs } = setup();
  const id = await ready(session, posted, procs);
  session.handle({ v: 1, type: 'send', conversationId: id, text: 'ask me' });
  await tick();
  const sid = procs[0].commands[0].session_id;
  procs[0].emitFrames([
    { type: 'ack', ref: 'message' },
    { type: 'interrupt', action_requests: [], allowed_decisions: ['approve'] },
    ...endTurn(sid),
  ]);
  await tick();
  session.handle({ v: 1, type: 'decide', conversationId: id, decisions: [{ type: 'approve' }] });
  await tick();
  assert.deepEqual(procs[0].commands[1], { type: 'decision', decisions: [{ type: 'approve' }], session_id: sid });
  assert.ok(posted.some((m) => m.type === 'decision' && m.conversationId === id));
  session.handle({ v: 1, type: 'ui/ready' });
  const log = restoreOf(posted).transcripts[id];
  assert.ok(log.some((e) => e.kind === 'decision'));
  // Malformed decisions never reach the sidecar.
  procs[0].emitFrames(endTurn(sid));
  await tick();
  session.handle({ v: 1, type: 'decide', conversationId: id, decisions: [] });
  session.handle({ v: 1, type: 'decide', conversationId: id, decisions: [{ message: 'no type' }] });
  await tick();
  assert.equal(procs[0].commands.length, 2);
  session.dispose();
});
