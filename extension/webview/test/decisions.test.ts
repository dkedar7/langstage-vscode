// The approval card's decisions (M3), built with hitl.ts buildDecisions: `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import type { Frame } from '../../src/shared/panelProtocol';
import { cardVerbs, customDecisions, describeAnswer, editDecisions, simpleDecisions } from '../state/decisions';

const HITL: Frame = {
  type: 'interrupt',
  action_requests: [
    { name: 'write_file', args: { path: 'a.txt', content: 'hi' }, description: 'Write a file' },
    { name: 'delete_file', args: { path: 'b.txt' } },
  ],
  allowed_decisions: ['reject', 'edit', 'approve', 'respond', 'escalate'],
};

test('one button per allowed verb, canonical ones first, custom verbs kept', () => {
  assert.deepEqual(
    cardVerbs(HITL).map((v) => [v.verb, v.canon]),
    [
      ['approve', 'approve'],
      ['edit', 'edit'],
      ['respond', 'respond'],
      ['reject', 'reject'],
      ['escalate', undefined],
    ],
  );
  // Legacy aliases (core normalize_decision): the button keeps the frame's spelling,
  // and an alias and its canonical verb make one button.
  const legacy: Frame = { type: 'interrupt', allowed_decisions: ['accept', 'ignore', 'response', 'approve'] };
  assert.deepEqual(
    cardVerbs(legacy).map((v) => [v.verb, v.canon]),
    [
      ['accept', 'approve'],
      ['response', 'respond'],
      ['ignore', 'reject'],
    ],
  );
});

test('approve / reject / respond: one decision per action, in the frame spelling', () => {
  assert.deepEqual(simpleDecisions(HITL, 'approve'), { ok: true, decisions: [{ type: 'approve' }, { type: 'approve' }] });
  const reject = simpleDecisions(HITL, 'reject', 'too risky');
  assert.ok(reject.ok);
  assert.deepEqual(reject.decisions[0], { type: 'reject', message: 'too risky' });
  assert.deepEqual(simpleDecisions(HITL, 'reject', '  '), { ok: true, decisions: [{ type: 'reject' }, { type: 'reject' }] });
  const respond = simpleDecisions(HITL, 'respond', 'call me Kedar');
  assert.ok(respond.ok);
  assert.deepEqual(respond.decisions[0], { type: 'respond', message: 'call me Kedar' });
  assert.equal(simpleDecisions(HITL, 'respond', '').ok, false);
  const legacy: Frame = { type: 'interrupt', allowed_decisions: ['accept'] };
  assert.deepEqual(simpleDecisions(legacy, 'approve'), { ok: true, decisions: [{ type: 'accept' }] });
  assert.equal(simpleDecisions(legacy, 'reject').ok, false, 'a verb the frame does not allow');
});

test('edit: the edited action gets edited_action, the others are approved', () => {
  const r = editDecisions(HITL, 1, '{"path": "c.txt"}');
  assert.ok(r.ok);
  assert.deepEqual(r.decisions, [
    { type: 'approve' },
    { type: 'edit', edited_action: { name: 'delete_file', args: { path: 'c.txt' } } },
  ]);
  const bad = editDecisions(HITL, 0, '{nope');
  assert.equal(bad.ok, false);
  assert.match(!bad.ok ? bad.reason : '', /not valid JSON/);
  assert.equal(editDecisions(HITL, 0, '[1]').ok, false);
  // No approve allowed: the other actions are "edited" with their own arguments.
  const editOnly: Frame = { ...HITL, allowed_decisions: ['edit'] };
  const r2 = editDecisions(editOnly, 0, '{"path": "z.txt", "content": "x"}');
  assert.ok(r2.ok);
  assert.deepEqual(r2.decisions[1], { type: 'edit', edited_action: { name: 'delete_file', args: { path: 'b.txt' } } });
});

test('custom verbs and the answer summary', () => {
  assert.deepEqual(customDecisions(HITL, 'escalate'), {
    ok: true,
    decisions: [{ type: 'escalate' }, { type: 'escalate' }],
  });
  assert.equal(describeAnswer([{ type: 'accept' }]), 'Approved');
  assert.equal(describeAnswer([{ type: 'reject', message: 'no' }]), 'Rejected: no');
  assert.equal(describeAnswer([{ type: 'response', message: 'Kedar' }]), 'Responded: Kedar');
  assert.equal(describeAnswer([{ type: 'approve' }, { type: 'edit', edited_action: {} }]), 'Approved with edited arguments');
  assert.equal(describeAnswer([{ type: 'escalate' }]), 'Answered: escalate');
});
