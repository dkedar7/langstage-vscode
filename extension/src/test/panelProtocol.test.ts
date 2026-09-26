// The webview ⇄ host protocol helpers: `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { LogEntry, appendLog, isOpenableUrl, parseWebviewMessage } from '../shared/panelProtocol';

test('parseWebviewMessage accepts every well-formed message', () => {
  const ok: unknown[] = [
    { v: 1, type: 'ui/ready' },
    { v: 1, type: 'send', conversationId: 'c', text: 'hi' },
    { v: 1, type: 'decide', conversationId: 'c', decisions: [{ type: 'approve' }] },
    { v: 1, type: 'cancel', conversationId: 'c' },
    { v: 1, type: 'conversation/new' },
    { v: 1, type: 'conversation/switch', conversationId: 'c' },
    { v: 1, type: 'conversation/delete', conversationId: 'c' },
    { v: 1, type: 'conversation/rename', conversationId: 'c', title: 't' },
    { v: 1, type: 'openExternal', url: 'https://x' },
    { v: 1, type: 'openSettings' },
    { v: 1, type: 'restartSidecar' },
    { v: 1, type: 'tryDemo' },
    { v: 1, type: 'copy', text: 'x' },
  ];
  for (const m of ok) assert.deepEqual(parseWebviewMessage(m), m);
});

test('parseWebviewMessage rejects wrong versions, types and field types', () => {
  const bad: unknown[] = [
    undefined,
    'send',
    { type: 'ui/ready' },
    { v: 2, type: 'ui/ready' },
    { v: 1, type: 'nope' },
    { v: 1, type: 'send', conversationId: 'c' },
    { v: 1, type: 'send', conversationId: 'c', text: 5 },
    { v: 1, type: 'decide', conversationId: 'c', decisions: 'approve' },
    { v: 1, type: 'decide', conversationId: 'c', decisions: [['approve']] },
    { v: 1, type: 'openExternal' },
  ];
  for (const m of bad) assert.equal(parseWebviewMessage(m), undefined, JSON.stringify(m));
});

test('parseWebviewMessage drops unknown extra keys', () => {
  assert.deepEqual(parseWebviewMessage({ v: 1, type: 'cancel', conversationId: 'c', evil: true }), {
    v: 1,
    type: 'cancel',
    conversationId: 'c',
  });
});

test('isOpenableUrl allows http(s) and mailto only', () => {
  assert.ok(isOpenableUrl('https://example.com'));
  assert.ok(isOpenableUrl('http://localhost:3000'));
  assert.ok(isOpenableUrl('mailto:a@b.c'));
  for (const u of ['javascript:alert(1)', 'command:x', 'file:///etc/passwd', 'vscode://x', 'not a url']) {
    assert.ok(!isOpenableUrl(u), u);
  }
});

test('appendLog joins content chunks of one message and consecutive reasoning', () => {
  const log: LogEntry[] = [];
  appendLog(log, { kind: 'user', text: 'hi' });
  appendLog(log, { kind: 'frame', frame: { type: 'reasoning', content: 'a ' } });
  appendLog(log, { kind: 'frame', frame: { type: 'reasoning', content: 'b' } });
  appendLog(log, { kind: 'frame', frame: { type: 'content', content: 'x', message_id: 'm1' } });
  appendLog(log, { kind: 'frame', frame: { type: 'content', content: 'y', message_id: 'm1' } });
  appendLog(log, { kind: 'frame', frame: { type: 'content', content: 'z', message_id: 'm2' } });
  appendLog(log, { kind: 'frame', frame: { type: 'turn_end' } });
  assert.deepEqual(log, [
    { kind: 'user', text: 'hi' },
    { kind: 'frame', frame: { type: 'reasoning', content: 'a b' } },
    { kind: 'frame', frame: { type: 'content', content: 'xy', message_id: 'm1' } },
    { kind: 'frame', frame: { type: 'content', content: 'z', message_id: 'm2' } },
    { kind: 'frame', frame: { type: 'turn_end' } },
  ]);
});
