// The panel reducer's approval (M3) and conversation (M4) behavior: `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import type { Frame, HostToWebview, LogEntry } from '../../src/shared/panelProtocol';
import { PanelState, initialState, reduce } from '../state/reducer';

const C = 'c1';

function restored(transcript: LogEntry[] = []): PanelState {
  return reduce(initialState, {
    v: 1,
    type: 'restore',
    conversations: [{ id: C, title: 't' }],
    activeId: C,
    transcripts: { [C]: transcript },
    turns: {},
    status: { phase: 'ready' },
  });
}

// What `--demo=tools` sends for "ask me" (recorded from the real sidecar).
const ASK: Frame = {
  type: 'interrupt',
  action_requests: [{ action: 'ask_user', args: { question: 'What should I call you?' } }],
  review_configs: [],
  allowed_decisions: ['respond', 'approve'],
};

const frameMsg = (frame: Frame): HostToWebview => ({ v: 1, type: 'frame', conversationId: C, frame });

function paused(): PanelState {
  let s = restored();
  s = reduce(s, { v: 1, type: 'user', conversationId: C, text: 'ask me', at: 1 });
  for (const f of [{ type: 'ack', ref: 'message' }, ASK, { type: 'complete', outcome: 'interrupted' }, { type: 'turn_end' }]) {
    s = reduce(s, frameMsg(f));
  }
  return s;
}

test('an interrupt makes the conversation pending, on that card', () => {
  const conv = paused().conversations[C];
  const card = conv.items.find((i) => i.kind === 'interrupt');
  assert.ok(card && conv.pending);
  assert.equal(conv.pending.key, card.key);
});

test('an accepted decision (ack) clears the pending interrupt and records the answer on its card', () => {
  let s = paused();
  s = reduce(s, { v: 1, type: 'decision', conversationId: C, decisions: [{ type: 'approve' }], at: 2 });
  assert.deepEqual(s.conversations[C].pending?.sent, [{ type: 'approve' }]);
  s = reduce(s, frameMsg({ type: 'ack', ref: 'decision' }));
  s = reduce(s, frameMsg({ type: 'content', content: 'Resumed.', message_id: 'm2' }));
  const conv = s.conversations[C];
  assert.equal(conv.pending, undefined);
  const item = conv.items.find((i) => i.kind === 'interrupt');
  assert.ok(item && item.kind === 'interrupt');
  assert.deepEqual(item.answer, [{ type: 'approve' }]);
  assert.equal(conv.items[conv.items.length - 1].kind, 'assistant');
});

test('a refused decision (error → turn_end, no ack) keeps the card live and shows why', () => {
  let s = paused();
  s = reduce(s, { v: 1, type: 'decision', conversationId: C, decisions: [{ type: 'reject' }], at: 2 });
  s = reduce(s, frameMsg({ type: 'error', error: "decision 'reject' is not allowed by the pending interrupt" }));
  s = reduce(s, frameMsg({ type: 'turn_end' }));
  let conv = s.conversations[C];
  assert.ok(conv.pending, 'still pending');
  assert.match(conv.pending.error ?? '', /not allowed/);
  assert.equal(conv.pending.sent, undefined);
  assert.ok(!conv.items.some((i) => i.kind === 'error'), 'shown on the card, not as a transcript error');
  // A valid answer afterwards clears the error and resolves it.
  s = reduce(s, { v: 1, type: 'decision', conversationId: C, decisions: [{ type: 'accept' }], at: 3 });
  assert.equal(s.conversations[C].pending?.error, undefined);
  s = reduce(s, frameMsg({ type: 'ack', ref: 'decision' }));
  conv = s.conversations[C];
  assert.equal(conv.pending, undefined);
});

test('a message refused while paused (gh #134) leaves the interrupt pending', () => {
  let s = paused();
  s = reduce(s, { v: 1, type: 'user', conversationId: C, text: 'hello', at: 2 });
  s = reduce(s, frameMsg({ type: 'error', error: "session 's1' is paused on a pending interrupt" }));
  s = reduce(s, frameMsg({ type: 'turn_end' }));
  assert.match(s.conversations[C].pending?.error ?? '', /paused/);
});

test('a decision stopped before it reached the sidecar is no longer "sending"', () => {
  let s = paused();
  s = reduce(s, { v: 1, type: 'decision', conversationId: C, decisions: [{ type: 'approve' }], at: 2 });
  s = reduce(s, frameMsg({ type: 'cancelled', source: 'panel' }));
  s = reduce(s, { v: 1, type: 'turn/ended', conversationId: C });
  assert.ok(s.conversations[C].pending);
  assert.equal(s.conversations[C].pending?.sent, undefined);
});

test('a host-side error while pending is a transcript error, not a refusal', () => {
  let s = paused();
  s = reduce(s, frameMsg({ type: 'error', error: 'sidecar exited mid-turn', source: 'panel' }));
  const conv = s.conversations[C];
  assert.equal(conv.items[conv.items.length - 1].kind, 'error');
  assert.equal(conv.pending?.error, undefined);
});

test('memory_reset expires the pending interrupt and marks where memory stops', () => {
  let s = paused();
  s = reduce(s, frameMsg({ type: 'memory_reset', source: 'panel' }));
  const conv = s.conversations[C];
  assert.equal(conv.pending, undefined);
  const card = conv.items.find((i) => i.kind === 'interrupt');
  assert.ok(card && card.kind === 'interrupt' && card.expired);
  assert.equal(conv.items[conv.items.length - 1].kind, 'memoryReset');
});

test('restore replays decisions: the answered card and the pending one come back the same', () => {
  const log: LogEntry[] = [
    { kind: 'user', text: 'ask me' },
    { kind: 'frame', frame: { type: 'ack', ref: 'message' } },
    { kind: 'frame', frame: ASK },
    { kind: 'frame', frame: { type: 'turn_end' } },
    { kind: 'decision', decisions: [{ type: 'respond', message: 'Kedar' }] },
    { kind: 'frame', frame: { type: 'ack', ref: 'decision' } },
    { kind: 'frame', frame: { type: 'content', content: 'Resumed.' } },
    { kind: 'frame', frame: { type: 'turn_end' } },
    { kind: 'user', text: 'ask me again' },
    { kind: 'frame', frame: { type: 'ack', ref: 'message' } },
    { kind: 'frame', frame: ASK },
    { kind: 'frame', frame: { type: 'turn_end' } },
  ];
  const conv = restored(log).conversations[C];
  const cards = conv.items.filter((i) => i.kind === 'interrupt');
  assert.equal(cards.length, 2);
  assert.ok(cards[0].kind === 'interrupt');
  assert.deepEqual(cards[0].answer, [{ type: 'respond', message: 'Kedar' }]);
  assert.equal(conv.pending?.key, cards[1].key);
});

test('restore orders conversations newest first and carries persistence and turn state', () => {
  const s = reduce(initialState, {
    v: 1,
    type: 'restore',
    conversations: [
      { id: 'a', title: 'A', updatedAt: 1 },
      { id: 'b', title: 'B', updatedAt: 3 },
      { id: 'c', title: 'C', updatedAt: 2 },
    ],
    activeId: 'a',
    transcripts: {},
    turns: { c: 'queued' },
    status: { phase: 'ready' },
    persistent: true,
  });
  assert.deepEqual(s.order, ['b', 'c', 'a']);
  assert.equal(s.persistent, true);
  assert.equal(s.conversations.c.turn, 'queued');
});
