// SidecarClient tests against a fake child process (no Python needed): `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { SidecarClient, SidecarFrame, isNoAgentError, sidecarArgs } from '../sidecar';
import { demoToolsTurns, fakeSpawn, tick } from './fakeSidecar';

function makeClient() {
  const { spawn, procs } = fakeSpawn();
  const client = new SidecarClient({ python: 'python', args: [], cwd: '.', spawn, exitGraceMs: 20 });
  return { client, procs };
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
