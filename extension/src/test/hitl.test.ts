// Unit tests for the chat panel's HITL logic (no VS Code needed): `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import {
  buildDecisions,
  buttonQuery,
  buttonTitle,
  canonicalVerb,
  isDecisionCommand,
  pendingFromFrame,
  pendingFromMetadata,
  summarizeActions,
} from '../hitl';

const middlewareFrame = {
  type: 'interrupt',
  action_requests: [{ name: 'delete_file', args: { path: 'x.txt' }, description: 'Delete x.txt?' }],
  allowed_decisions: ['reject', 'respond', 'approve'],
};

test('pending interrupt comes from the frame and survives result metadata', () => {
  const pending = pendingFromFrame(middlewareFrame);
  assert.deepEqual(pending, { allowed: ['reject', 'respond', 'approve'], actionCount: 1 });
  // ChatResult metadata is JSON round-tripped by VS Code.
  assert.deepEqual(pendingFromMetadata(JSON.parse(JSON.stringify(pending))), pending);
  assert.equal(pendingFromMetadata(undefined), undefined);
  assert.equal(pendingFromMetadata({ allowed: 'approve' }), undefined);
});

test('slash commands map to decisions in the frame spelling', () => {
  const pending = pendingFromFrame(middlewareFrame);
  assert.deepEqual(buildDecisions('approve', '', pending), { ok: true, decisions: [{ type: 'approve' }] });
  assert.deepEqual(buildDecisions('reject', '', pending), { ok: true, decisions: [{ type: 'reject' }] });
  assert.deepEqual(buildDecisions('reject', ' not now ', pending), {
    ok: true,
    decisions: [{ type: 'reject', message: 'not now' }],
  });
  assert.deepEqual(buildDecisions('respond', 'use y.txt', pending), {
    ok: true,
    decisions: [{ type: 'respond', message: 'use y.txt' }],
  });
});

test('legacy aliases are sent as the frame advertises them', () => {
  const legacy = { allowed: ['accept', 'ignore', 'response'], actionCount: 1 };
  assert.deepEqual(buildDecisions('approve', '', legacy), { ok: true, decisions: [{ type: 'accept' }] });
  assert.deepEqual(buildDecisions('respond', 'hi', legacy), {
    ok: true,
    decisions: [{ type: 'response', message: 'hi' }],
  });
  assert.equal(canonicalVerb('IGNORE'), 'reject');
  assert.equal(canonicalVerb('custom'), undefined);
});

test('a verb the interrupt does not allow, or a missing payload, is refused locally', () => {
  const approveOnly = { allowed: ['approve'], actionCount: 1 };
  const r = buildDecisions('reject', '', approveOnly);
  assert.equal(r.ok, false);
  assert.match((r as { reason: string }).reason, /allows: approve/);
  assert.equal(buildDecisions('approve', 'junk', approveOnly).ok, false);
  const any = { allowed: ['respond', 'edit'], actionCount: 1 };
  assert.equal(buildDecisions('respond', '  ', any).ok, false);
  assert.equal(buildDecisions('edit', '', any).ok, false);
  assert.equal(buildDecisions('edit', 'not json', any).ok, false);
  assert.equal(buildDecisions('edit', '[1]', any).ok, false);
});

test('edit merges a JSON object; the verb wins the type key', () => {
  const pending = { allowed: ['edit'], actionCount: 1 };
  const edited = { edited_action: { name: 'delete_file', args: { path: 'y.txt' } }, type: 'x' };
  assert.deepEqual(buildDecisions('edit', JSON.stringify(edited), pending), {
    ok: true,
    decisions: [{ edited_action: edited.edited_action, type: 'edit' }],
  });
});

test('one decision per requested action', () => {
  const frame = { action_requests: [{ name: 'a' }, { name: 'b' }], allowed_decisions: ['approve'] };
  const r = buildDecisions('approve', '', pendingFromFrame(frame));
  assert.deepEqual(r, { ok: true, decisions: [{ type: 'approve' }, { type: 'approve' }] });
});

test('action summaries read all three wire shapes', () => {
  assert.deepEqual(summarizeActions(middlewareFrame), [
    { name: 'delete_file', description: 'Delete x.txt?', args: { path: 'x.txt' } },
  ]);
  assert.deepEqual(summarizeActions({ action_requests: [{ action: 'ask_user', args: { q: 1 } }] }), [
    { name: 'ask_user', description: undefined, args: { q: 1 } },
  ]);
  assert.deepEqual(
    summarizeActions({ action_requests: [{ action_request: { action: 'confirm' }, description: 'Sure?' }] }),
    [{ name: 'confirm', description: 'Sure?', args: undefined }],
  );
  assert.deepEqual(summarizeActions({}), [{ name: 'an action' }]);
});

test('buttons submit approve/reject and prefill respond/edit', () => {
  assert.deepEqual(buttonQuery('approve'), { query: '@langstage /approve', isPartialQuery: false });
  assert.deepEqual(buttonQuery('accept'), { query: '@langstage /approve', isPartialQuery: false });
  assert.deepEqual(buttonQuery('respond'), { query: '@langstage /respond ', isPartialQuery: true });
  assert.equal(buttonQuery('custom'), undefined);
  assert.equal(buttonTitle('respond'), 'Respond…');
  assert.ok(isDecisionCommand('edit'));
  assert.ok(!isDecisionCommand(undefined));
  assert.ok(!isDecisionCommand('accept'));
});
