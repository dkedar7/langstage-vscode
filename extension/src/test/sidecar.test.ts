// SidecarClient tests against a fake child process (no Python needed): `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import {
  MIN_SIDECAR_VERSION,
  SUPPORTED_PROTOCOL,
  SidecarClient,
  SidecarFrame,
  SidecarInfo,
  SidecarOptions,
  compareVersions,
  isNoAgentError,
  readyCheckpointer,
  readyInfo,
  sidecarArgs,
  sidecarCompatibility,
  supports,
} from '../sidecar';
import { demoToolsTurns, fakeSpawn, tick } from './fakeSidecar';

function makeClient(extra: Partial<SidecarOptions> = {}) {
  const { spawn, procs } = fakeSpawn();
  const client = new SidecarClient({ python: 'python', args: [], cwd: '.', spawn, exitGraceMs: 20, ...extra });
  return { client, procs };
}

/** The `ready` frame sidecar 0.5.35 sends (gh #89). */
const CAPABILITIES = ['message', 'decision', 'cancel', 'shutdown', 'checkpointer'];
function readyFrame(extra: Record<string, unknown> = {}): SidecarFrame {
  return { type: 'ready', version: '0.5.35', protocol: 1, capabilities: CAPABILITIES, ...extra };
}

test('sidecarArgs: agent spec, demo, and demo wins over a spec', () => {
  assert.deepEqual(sidecarArgs('/w', ''), ['-m', 'langstage_vscode', '--workspace', '/w']);
  assert.deepEqual(sidecarArgs('/w', 'a.py:g'), ['-m', 'langstage_vscode', '--workspace', '/w', '--agent', 'a.py:g']);
  assert.deepEqual(sidecarArgs('/w', 'a.py:g', 'tools'), ['-m', 'langstage_vscode', '--workspace', '/w', '--demo=tools']);
  assert.deepEqual(sidecarArgs('/w', '', 'echo'), ['-m', 'langstage_vscode', '--workspace', '/w', '--demo']);
});

test('isNoAgentError recognizes the sidecar no-spec startup error', () => {
  assert.ok(isNoAgentError('no agent spec (pass --agent or --demo, set LANGSTAGE_AGENT_SPEC, ...)'));
  assert.ok(!isNoAgentError('could not import my_agent'));
  assert.ok(!isNoAgentError(undefined));
});

test('a turn waits for ready, then streams its frames and resolves on turn_end', async () => {
  const { client, procs } = makeClient();
  const seen: SidecarFrame[] = [];
  const statuses: string[] = [];
  client.onStatus((s) => statuses.push(s.state));
  const turn = client.runTurn('s1', { type: 'message', content: 'please use a tool' }, {
    onFrame: (f) => seen.push(f),
  });
  await tick();
  const proc = procs[0];
  assert.equal(proc.commands.length, 0, 'nothing is written before ready');
  proc.emitFrames([{ type: 'ready' }]);
  await tick();
  assert.deepEqual(proc.commands[0], { type: 'message', content: 'please use a tool', session_id: 's1' });
  const [toolTurn] = demoToolsTurns();
  proc.emitFrames(toolTurn);
  const result = await turn.done;
  assert.equal(result.acked, true);
  assert.equal(result.interrupt, undefined);
  assert.deepEqual(seen.map((f) => f.type), toolTurn.map((f) => f.type));
  assert.deepEqual(statuses, ['starting', 'ready']);
  client.dispose();
});

test('turns are serialized: the second command waits for the first turn_end', async () => {
  const { client, procs } = makeClient();
  const started: string[] = [];
  const a = client.runTurn('A', { type: 'message', content: 'one' }, { onStart: () => started.push('A') });
  const b = client.runTurn('B', { type: 'message', content: 'two' }, { onStart: () => started.push('B') });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  assert.deepEqual(started, ['A']);
  assert.deepEqual(client.queuedSessionIds, ['B']);
  assert.equal(procs[0].commands.length, 1);
  procs[0].emitFrames([{ type: 'ack', ref: 'message' }, { type: 'complete' }, { type: 'turn_end', session_id: 'A' }]);
  await a.done;
  await tick();
  assert.deepEqual(started, ['A', 'B']);
  assert.equal(procs[0].commands[1].session_id, 'B');
  procs[0].emitFrames([{ type: 'turn_end', session_id: 'B' }]);
  assert.equal((await b.done).acked, false);
  client.dispose();
});

test('gh #152: the ready status carries the checkpointer the ready frame reports', async () => {
  const { client, procs } = makeClient();
  const ready = client.start();
  await tick();
  procs[0].emitFrames([{ type: 'ready', checkpointer: { kind: 'SqliteSaver', durable: true } }]);
  await ready;
  assert.deepEqual(client.status, { state: 'ready', info: {}, checkpointer: { kind: 'SqliteSaver', durable: true } });
  client.dispose();
});

test('gh #152: an older sidecar (bare ready) leaves the checkpointer unknown', async () => {
  const { client, procs } = makeClient();
  const ready = client.start();
  await tick();
  procs[0].emitFrames([{ type: 'ready' }]);
  await ready;
  assert.deepEqual(client.status, { state: 'ready', info: {} });
  client.dispose();
});

test('readyCheckpointer accepts the documented shape and rejects anything else', () => {
  assert.deepEqual(readyCheckpointer({ type: 'ready', checkpointer: { kind: 'InMemorySaver', durable: false } }), {
    kind: 'InMemorySaver',
    durable: false,
  });
  assert.deepEqual(readyCheckpointer({ type: 'ready', checkpointer: { kind: null, durable: false } }), {
    kind: null,
    durable: false,
  });
  assert.equal(readyCheckpointer({ type: 'ready' }), undefined);
  assert.equal(readyCheckpointer({ type: 'ready', checkpointer: 'SqliteSaver' }), undefined);
  assert.equal(readyCheckpointer({ type: 'ready', checkpointer: [true] }), undefined);
  assert.equal(readyCheckpointer({ type: 'ready', checkpointer: { kind: 'X', durable: 'yes' } }), undefined);
});

test('an interrupt turn reports the interrupt frame', async () => {
  const { client, procs } = makeClient();
  const turn = client.runTurn('s1', { type: 'message', content: 'ask me' });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }, ...demoToolsTurns()[2]]);
  const result = await turn.done;
  assert.deepEqual(result.interrupt?.allowed_decisions, ['respond', 'approve']);
  client.dispose();
});

test('gh #131: the pre-ready error frame is the startup error', async () => {
  const { client, procs } = makeClient();
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  procs[0].emitFrames([{ type: 'error', error: 'no agent spec (pass --agent or --demo)' }]);
  procs[0].exit(1);
  await assert.rejects(turn.done, /no agent spec/);
  await assert.rejects(client.start(), /no agent spec/);
  assert.equal(client.status.state, 'failed');
  assert.equal(client.alive, false);
});

test('a sidecar that dies before any frame reports the last stderr line', async () => {
  const { client, procs } = makeClient();
  const ready = client.start();
  await tick();
  procs[0].stderr.write('Traceback...\nModuleNotFoundError: No module named langstage_vscode\n');
  await tick();
  procs[0].exit(1);
  await assert.rejects(ready, /before it was ready: ModuleNotFoundError/);
  assert.match(client.status.stderrTail ?? '', /ModuleNotFoundError/);
});

test('cancel: the active turn gets a cooperative cancel; a queued one never reaches the sidecar', async () => {
  const { client, procs } = makeClient();
  const a = client.runTurn('A', { type: 'message', content: 'one' });
  const b = client.runTurn('B', { type: 'message', content: 'two' });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }]);
  await tick();
  assert.equal(client.cancel('B'), true);
  assert.deepEqual(await b.done, { acked: false, cancelledBeforeStart: true });
  assert.equal(client.cancel('A'), true);
  await tick();
  assert.deepEqual(procs[0].commands.map((c) => c.type), ['message', 'cancel']);
  assert.equal(procs[0].commands[1].session_id, 'A');
  procs[0].emitFrames([{ type: 'cancelled', session_id: 'A' }, { type: 'turn_end', session_id: 'A' }]);
  await a.done;
  assert.equal(client.cancel('nobody'), false);
  assert.equal(client.alive, true, 'cancel keeps the process (and its memory)');
  client.dispose();
});

test('a sidecar that exits mid-turn rejects the turn and reports stopped', async () => {
  const { client, procs } = makeClient();
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  procs[0].emitFrames([{ type: 'ready' }, { type: 'ack', ref: 'message' }]);
  await tick();
  procs[0].exit(1);
  await assert.rejects(turn.done, /exited mid-turn/);
  assert.equal(client.status.state, 'stopped');
  await assert.rejects(client.runTurn('s1', { type: 'message', content: 'again' }).done, /not available/);
});

test('a frame with no turn in flight goes to onStrayFrame', async () => {
  const { client, procs } = makeClient();
  const stray: SidecarFrame[] = [];
  client.onStrayFrame((f) => stray.push(f));
  await Promise.all([client.start(), (async () => { await tick(); procs[0].emitFrames([{ type: 'ready' }]); })()]);
  procs[0].emitFrames([{ type: 'error', error: "no turn in progress for session 'x'" }]);
  await tick();
  assert.equal(stray.length, 1);
  client.dispose();
});

test('dispose fails pending turns and kills the process', async () => {
  const { client, procs } = makeClient();
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  client.dispose();
  await assert.rejects(turn.done, /stopped/);
  assert.equal(procs[0].killed, true);
  assert.equal(client.alive, false);
});

// ------------------------------------------------------------------ gh #89: the handshake

test('gh #89: readyInfo reads version, protocol and capabilities', () => {
  assert.deepEqual(readyInfo(readyFrame()), { version: '0.5.35', protocol: 1, capabilities: CAPABILITIES });
  assert.deepEqual(readyInfo({ type: 'ready' }), {}, 'an older sidecar sends none');
  assert.deepEqual(readyInfo({ type: 'ready', version: ' 0.6.0 ' }), { version: '0.6.0' });
});

test('gh #89: readyInfo leaves out malformed fields', () => {
  assert.deepEqual(readyInfo({ type: 'ready', version: 35, protocol: '2', capabilities: 'cancel' }), {});
  assert.deepEqual(readyInfo({ type: 'ready', version: '', protocol: null, capabilities: null }), {});
  assert.deepEqual(readyInfo({ type: 'ready', version: ['0.5.35'], protocol: 1.5 }), {});
  assert.deepEqual(readyInfo({ type: 'ready', protocol: 0 }), {});
  assert.deepEqual(readyInfo({ type: 'ready', protocol: -2 }), {});
  assert.deepEqual(readyInfo({ type: 'ready', capabilities: { cancel: true } }), {});
  assert.deepEqual(readyInfo({ type: 'ready', capabilities: ['cancel', 3, null, 'message'] }), {
    capabilities: ['cancel', 'message'],
  });
  assert.deepEqual(readyInfo({ type: 'ready', capabilities: [] }), { capabilities: [] });
});

test('gh #89: compareVersions compares dotted versions numerically', () => {
  assert.ok(compareVersions('0.5.9', '0.5.35') < 0, 'numeric, not lexicographic');
  assert.ok(compareVersions('0.5.35', '0.5.9') > 0);
  assert.ok(compareVersions('0.6.0', '0.5.35') > 0);
  assert.ok(compareVersions('1.0', '0.99.99') > 0);
  assert.equal(compareVersions('0.5.35', '0.5.35'), 0);
  assert.equal(compareVersions('0.6', '0.6.0'), 0, 'missing parts are 0');
  assert.equal(compareVersions('0.6.0.0', '0.6'), 0);
  assert.ok(compareVersions('0.5', '0.5.1') < 0);
  assert.equal(compareVersions('0.5.35rc1', '0.5.35'), 0, 'a pre-release suffix is ignored');
  assert.equal(compareVersions('0.5.35-beta.2', '0.5.35'), 0);
  assert.equal(compareVersions('0.5.35+local', '0.5.35'), 0);
  assert.equal(compareVersions('v0.5.35', '0.5.35'), 0);
  assert.ok(compareVersions('0.5.34.dev1', '0.5.35') < 0);
});

test('gh #89: sidecarCompatibility says ok, outdated or incompatible', () => {
  assert.equal(MIN_SIDECAR_VERSION, '0.5.35');
  assert.equal(SUPPORTED_PROTOCOL, 1);
  assert.deepEqual(sidecarCompatibility({ version: '0.5.35', protocol: 1 }), { kind: 'ok' });
  assert.deepEqual(sidecarCompatibility({ version: '0.6.0', protocol: 1 }), { kind: 'ok' });
  assert.deepEqual(sidecarCompatibility({ version: '0.5.35' }), { kind: 'ok' }, 'a missing protocol is 1');
  assert.deepEqual(sidecarCompatibility({ version: '0.5.9', protocol: 1 }), { kind: 'outdated', version: '0.5.9' });
  assert.deepEqual(sidecarCompatibility({ version: '0.5.34' }), { kind: 'outdated', version: '0.5.34' });
  assert.deepEqual(sidecarCompatibility({}), { kind: 'outdated' }, 'no version: older than 0.5.35');
  assert.deepEqual(sidecarCompatibility({ protocol: 1, capabilities: [] }), { kind: 'outdated' });
  assert.deepEqual(sidecarCompatibility({ version: 'unknown' }), { kind: 'outdated', version: 'unknown' });
  assert.deepEqual(sidecarCompatibility({ version: '0.7.0', protocol: 2 }), { kind: 'incompatible', protocol: 2 });
  // A newer protocol is refused whatever the version says.
  assert.deepEqual(sidecarCompatibility({ protocol: 3 }), { kind: 'incompatible', protocol: 3 });
  assert.deepEqual(sidecarCompatibility({ version: '0.5.1', protocol: 2 }), { kind: 'incompatible', protocol: 2 });
});

test('gh #89: supports is true when capabilities are absent, else checks the list', () => {
  assert.equal(supports(undefined, 'cancel'), true, 'not ready yet');
  assert.equal(supports({}, 'cancel'), true, 'an older sidecar keeps the old behavior');
  assert.equal(supports({ version: '0.5.34' }, 'cancel'), true);
  assert.equal(supports({ capabilities: CAPABILITIES }, 'cancel'), true);
  assert.equal(supports({ capabilities: ['message', 'decision'] }, 'cancel'), false);
  assert.equal(supports({ capabilities: [] }, 'cancel'), false);
});

test('gh #89: the ready status and client.info carry the handshake; onReady gets it', async () => {
  const seen: SidecarInfo[] = [];
  const { client, procs } = makeClient({ onReady: (info) => seen.push(info) });
  assert.equal(client.info, undefined, 'unknown before ready');
  const ready = client.start();
  await tick();
  procs[0].emitFrames([readyFrame({ checkpointer: { kind: 'InMemorySaver', durable: false } })]);
  await ready;
  const info = { version: '0.5.35', protocol: 1, capabilities: CAPABILITIES };
  assert.deepEqual(client.status, {
    state: 'ready',
    info,
    checkpointer: { kind: 'InMemorySaver', durable: false },
  });
  assert.deepEqual(client.info, info);
  assert.deepEqual(seen, [info]);
  client.dispose();
});

test('gh #89: an outdated sidecar still runs, and onReady reports it', async () => {
  const seen: SidecarInfo[] = [];
  const { client, procs } = makeClient({ onReady: (info) => seen.push(info) });
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  procs[0].emitFrames([{ type: 'ready', version: '0.5.20' }]);
  await tick();
  assert.equal(client.status.state, 'ready');
  assert.deepEqual(seen, [{ version: '0.5.20' }]);
  assert.equal(procs[0].commands[0].type, 'message', 'the turn was sent');
  procs[0].emitFrames([{ type: 'ack', ref: 'message' }, { type: 'turn_end', session_id: 's1' }]);
  assert.equal((await turn.done).acked, true);
  client.dispose();
});

test('gh #89: an onReady that throws does not wedge the first turn', async () => {
  const { client, procs } = makeClient({
    onReady: () => {
      throw new Error('notification bug');
    },
  });
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  procs[0].emitFrames([readyFrame()]);
  await tick();
  assert.equal(procs[0].commands.length, 1);
  procs[0].emitFrames([{ type: 'turn_end', session_id: 's1' }]);
  await turn.done;
  client.dispose();
});

test('gh #89: a sidecar with a newer protocol is refused: failed status, no command, process stopped', async () => {
  const seen: SidecarInfo[] = [];
  const statuses: string[] = [];
  const { spawn, procs } = fakeSpawn();
  const client = new SidecarClient({
    python: '/venv/bin/python',
    args: [],
    cwd: '.',
    spawn,
    exitGraceMs: 20,
    onReady: (info) => seen.push(info),
  });
  client.onStatus((s) => statuses.push(s.state));
  const turn = client.runTurn('s1', { type: 'message', content: 'hi' });
  await tick();
  procs[0].emitFrames([readyFrame({ version: '0.9.0', protocol: 2 })]);
  await assert.rejects(turn.done, /speaks protocol 2.*supports protocol 1.*Update the LangStage extension/);
  await assert.rejects(client.start(), /speaks protocol 2/);
  const status = client.status;
  assert.equal(status.state, 'failed');
  assert.match(status.error ?? '', /sidecar in \/venv\/bin\/python \(0\.9\.0\) speaks protocol 2/);
  assert.equal(status.info, undefined);
  assert.equal(client.info, undefined);
  assert.equal(client.alive, false);
  assert.deepEqual(procs[0].commands.map((c) => c.type), ['shutdown'], 'only the shutdown, never the message');
  assert.equal(procs[0].killed, true);
  assert.deepEqual(seen, [], 'onReady is not called for a refused sidecar');
  assert.deepEqual(statuses, ['starting', 'failed']);
});

test('gh #89: cancel is sent to a sidecar that lists it', async () => {
  const { client, procs } = makeClient();
  const turn = client.runTurn('A', { type: 'message', content: 'one' });
  await tick();
  procs[0].emitFrames([readyFrame()]);
  await tick();
  assert.equal(client.cancel('A'), true);
  await tick();
  assert.deepEqual(procs[0].commands.map((c) => c.type), ['message', 'cancel']);
  procs[0].emitFrames([{ type: 'cancelled', session_id: 'A' }, { type: 'turn_end', session_id: 'A' }]);
  await turn.done;
  assert.equal(client.alive, true);
  client.dispose();
});

test('gh #89: a sidecar that does not list cancel gets none: the turn ends here and the process stops', async () => {
  const statuses: string[] = [];
  const { client, procs } = makeClient();
  client.onStatus((s) => statuses.push(s.state));
  const turn = client.runTurn('A', { type: 'message', content: 'one' });
  await tick();
  procs[0].emitFrames([readyFrame({ capabilities: ['message', 'decision', 'shutdown'] })]);
  await tick();
  procs[0].emitFrames([{ type: 'ack', ref: 'message' }]);
  await tick();
  assert.equal(client.cancel('A'), true);
  const result = await turn.done;
  assert.equal(result.acked, true);
  await tick();
  assert.deepEqual(
    procs[0].commands.map((c) => c.type),
    ['message', 'shutdown'],
    'no cancel command, only the shutdown that stops the process',
  );
  assert.equal(procs[0].killed, true);
  assert.equal(client.alive, false);
  assert.deepEqual(statuses, ['starting', 'ready', 'stopped']);
});
