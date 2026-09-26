// The panel reducer against the recorded `--demo=tools` transcript and synthetic
// frames: `npm test` (bundled by `node esbuild.mjs --tests`).
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import type { Frame, HostToWebview, LogEntry } from '../../src/shared/panelProtocol';
import { appendLog } from '../../src/shared/panelProtocol';
import {
  ConversationView,
  PanelState,
  emptyConversation,
  initialState,
  normalizeTodos,
  pendingInterrupt,
  reduce,
  reduceFrame,
} from '../state/reducer';

const C = 'c1';

function demoFrames(): Frame[] {
  return fs
    .readFileSync(path.resolve('src/test/fixtures/demo-tools.ndjson'), 'utf8')
    .split(/\r?\n/)
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as Frame);
}

function restored(transcript: LogEntry[] = [], turns: Record<string, 'queued' | 'running'> = {}): PanelState {
  return reduce(initialState, {
    v: 1,
    type: 'restore',
    conversations: [{ id: C, title: 't' }],
    activeId: C,
    transcripts: { [C]: transcript },
    turns,
    status: { phase: 'ready' },
  });
}

function fold(conv: ConversationView, frames: Frame[]): ConversationView {
  return frames.reduce(reduceFrame, conv);
}

const fresh = () => emptyConversation({ id: C, title: 't' });

test('the recorded demo transcript: tool card, reasoning, message boundaries, interrupt', () => {
  const conv = fold(fresh(), demoFrames());
  const kinds = conv.items.map((i) => i.kind);
  assert.deepEqual(kinds, ['tool', 'assistant', 'reasoning', 'assistant', 'interrupt']);

  const tool = conv.items[0];
  assert.ok(tool.kind === 'tool');
  assert.equal(tool.name, 'demo_lookup');
  assert.equal(tool.status, 'success');
  assert.deepEqual(tool.args, { query: 'please use a tool' });
  assert.equal(typeof tool.durationMs, 'number');
  assert.match(tool.result ?? '', /"answer": "42"/);
  assert.equal(tool.extraction?.type, 'demo_fact');

  const first = conv.items[1];
  assert.ok(first.kind === 'assistant');
  assert.match(first.text, /^The demo tool returned .* flow\.$/);
  assert.equal(first.streaming, false, 'complete / turn_end settle the stream');

  const reasoning = conv.items[2];
  assert.ok(reasoning.kind === 'reasoning');
  assert.match(reasoning.text, /^Let me reason about this step by step\. The demo streams/);

  const second = conv.items[3];
  assert.ok(second.kind === 'assistant');
  assert.notEqual(second.messageId, first.messageId);
  assert.match(second.text, /^Done reasoning/);
});

test('gh #108: a new message_id starts a new block; chunks without an id never break', () => {
  const conv = fold(fresh(), [
    { type: 'content', content: 'plan ', message_id: 'm1' },
    { type: 'content', content: 'ready.', message_id: 'm1' },
    { type: 'content', content: 'Answer', message_id: 'm2' },
    { type: 'content', content: '!' },
  ]);
  assert.deepEqual(
    conv.items.map((i) => (i.kind === 'assistant' ? i.text : i.kind)),
    ['plan ready.', 'Answer!'],
  );
  const [a, b] = conv.items;
  assert.ok(a.kind === 'assistant' && b.kind === 'assistant');
  assert.equal(a.streaming, false, 'the earlier block closes when the next starts');
  assert.equal(b.streaming, true);
});

test('tool_start / tool_end pair by id; an error result keeps the error message', () => {
  const conv = fold(fresh(), [
    { type: 'tool_start', id: 'a', name: 'ls', args: { path: '.' } },
    { type: 'tool_start', id: 'b', name: 'bash', args: { cmd: 'false' } },
    { type: 'tool_end', id: 'a', name: 'ls', result: 'x.txt', status: 'success', duration_ms: 3 },
    { type: 'tool_end', id: 'b', name: 'bash', result: '', status: 'error', error_message: 'exit 1', duration_ms: 1500 },
  ]);
  const [a, b] = conv.items;
  assert.ok(a.kind === 'tool' && b.kind === 'tool');
  assert.deepEqual([a.status, a.result, a.durationMs], ['success', 'x.txt', 3]);
  assert.deepEqual([b.status, b.errorMessage, b.durationMs], ['error', 'exit 1', 1500]);
});

test('a tool_end with no tool_start still shows a card', () => {
  const conv = fold(fresh(), [{ type: 'tool_end', id: 'z', name: 'snap', result: { ok: true }, status: 'success' }]);
  const [t] = conv.items;
  assert.ok(t.kind === 'tool');
  assert.equal(t.name, 'snap');
  assert.match(t.result ?? '', /"ok": true/);
});

test('todos: one live list per conversation, replaced in place by each extraction', () => {
  const conv = fold(fresh(), [
    { type: 'tool_start', id: 't1', name: 'write_todos', args: {} },
    { type: 'tool_end', id: 't1', name: 'write_todos', result: 'ok', status: 'success' },
    {
      type: 'extraction',
      tool_name: 'write_todos',
      extracted_type: 'todos',
      data: [
        { content: 'Plan', status: 'completed' },
        { content: 'Build', status: 'in_progress' },
      ],
    },
    { type: 'tool_start', id: 't2', name: 'write_todos', args: {} },
    { type: 'tool_end', id: 't2', name: 'write_todos', result: 'ok', status: 'success' },
    {
      type: 'extraction',
      tool_name: 'write_todos',
      extracted_type: 'todos',
      data: { todos: [{ task: 'Plan', done: true }, { task: 'Build', done: true }, 'Ship'] },
    },
  ]);
  assert.deepEqual(conv.todos, [
    { content: 'Plan', status: 'completed' },
    { content: 'Build', status: 'completed' },
    { content: 'Ship', status: 'pending' },
  ]);
  const second = conv.items[1];
  assert.ok(second.kind === 'tool');
  assert.equal(second.extraction?.type, 'todos', 'attached to the latest call of that tool');
});

test('normalizeTodos tolerates junk', () => {
  assert.deepEqual(normalizeTodos(null), []);
  assert.deepEqual(normalizeTodos('x'), []);
  assert.deepEqual(normalizeTodos([{ content: 'a', status: 'weird' }]), [{ content: 'a', status: 'pending' }]);
});

test('errors are inline with their traceback; host-side errors are marked', () => {
  const conv = fold(fresh(), [
    { type: 'content', content: 'partial', message_id: 'm' },
    { type: 'error', error: 'boom', traceback: 'Traceback (most recent call last): ...' },
    { type: 'error', error: 'sidecar exited mid-turn', source: 'panel' },
  ]);
  const [, e1, e2] = conv.items;
  assert.ok(e1.kind === 'error' && e2.kind === 'error');
  assert.deepEqual([e1.error, e1.traceback, e1.host], ['boom', 'Traceback (most recent call last): ...', false]);
  assert.equal(e2.host, true);
  assert.ok(conv.items[0].kind === 'assistant' && !conv.items[0].streaming);
});

test('cancelled settles the stream and leaves a notice', () => {
  const conv = fold(fresh(), [
    { type: 'content', content: 'half', message_id: 'm' },
    { type: 'cancelled', session_id: 's' },
    { type: 'turn_end', session_id: 's' },
  ]);
  assert.deepEqual(conv.items.map((i) => i.kind), ['assistant', 'notice']);
  assert.ok(conv.items[0].kind === 'assistant' && !conv.items[0].streaming);
});

test('unknown frame types and keys are ignored', () => {
  const before = fresh();
  assert.equal(reduceFrame(before, { type: 'usage', input_tokens: 3 }), before);
  assert.equal(reduceFrame(before, { type: 'something_new', x: 1 }), before);
  assert.equal(reduceFrame(before, { type: 'ack', ref: 'message' }), before);
  const conv = reduceFrame(before, { type: 'content', content: 'hi', message_id: 'm', future_key: [1] });
  assert.equal(conv.items.length, 1);
});

test('live messages: user → queued → started → frames → ended', () => {
  let s = restored();
  const msgs: HostToWebview[] = [
    { v: 1, type: 'user', conversationId: C, text: 'think about it', at: 1 },
    { v: 1, type: 'turn/queued', conversationId: C },
    { v: 1, type: 'turn/started', conversationId: C },
    { v: 1, type: 'frame', conversationId: C, frame: { type: 'reasoning', content: 'hmm' } },
  ];
  for (const m of msgs) s = reduce(s, m);
  assert.equal(s.conversations[C].turn, 'running');
  const r = s.conversations[C].items[1];
  assert.ok(r.kind === 'reasoning' && r.streaming);
  s = reduce(s, { v: 1, type: 'turn/ended', conversationId: C });
  assert.equal(s.conversations[C].turn, 'idle');
  const r2 = s.conversations[C].items[1];
  assert.ok(r2.kind === 'reasoning' && !r2.streaming);
  // Messages for an unknown conversation are dropped.
  assert.equal(reduce(s, { v: 1, type: 'turn/started', conversationId: 'nope' }), s);
});

test('restore from the host log renders the same transcript as the live frames', () => {
  const frames = demoFrames().filter((f) => f.type !== 'ready');
  const log: LogEntry[] = [];
  appendLog(log, { kind: 'user', text: 'please use a tool' });
  for (const f of frames) appendLog(log, { kind: 'frame', frame: f });
  assert.ok(log.length < frames.length / 2, 'the log coalesces token chunks');

  let live = restored();
  live = reduce(live, { v: 1, type: 'user', conversationId: C, text: 'please use a tool', at: 0 });
  for (const f of frames) live = reduce(live, { v: 1, type: 'frame', conversationId: C, frame: f });
  const replay = restored(log);
  const strip = (c: ConversationView) => c.items.map((i) => ({ ...i, key: 0, ...(i.kind === 'user' ? { at: 0 } : {}) }));
  assert.deepEqual(strip(replay.conversations[C]), strip(live.conversations[C]));
});

test('restore keeps a turn in flight streaming; an interrupt stays pending until an ack', () => {
  const s = restored([{ kind: 'frame', frame: { type: 'content', content: 'x', message_id: 'm' } }], { [C]: 'running' });
  const it = s.conversations[C].items[0];
  assert.ok(it.kind === 'assistant' && it.streaming);

  const paused = restored([{ kind: 'frame', frame: { type: 'interrupt', allowed_decisions: ['approve'] } }]);
  assert.equal(pendingInterrupt(paused.conversations[C])?.frame.type, 'interrupt');
  const busy = reduce(paused, { v: 1, type: 'turn/started', conversationId: C });
  assert.ok(pendingInterrupt(busy.conversations[C]), 'still pending while the answer is in flight');
  const acked = reduce(busy, { v: 1, type: 'frame', conversationId: C, frame: { type: 'ack', ref: 'decision' } });
  assert.equal(pendingInterrupt(acked.conversations[C]), undefined);
});
