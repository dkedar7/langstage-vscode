// The outdated-sidecar warning (gh #89): its text and command, and that it shows once
// per interpreter. No vscode needed: the notification is injected. `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { OutdatedNotice, OutdatedSidecarWarnings, outdatedNotice, pipUpgradeCommand } from '../outdatedSidecar';
import { SidecarClient } from '../sidecar';
import { fakeSpawn, tick } from './fakeSidecar';

const CURRENT = { version: '0.5.35', protocol: 1, capabilities: ['message', 'decision', 'cancel', 'shutdown'] };

test('pipUpgradeCommand runs pip through the configured interpreter, quoting a path with spaces', () => {
  assert.equal(pipUpgradeCommand('python'), 'python -m pip install -U langstage-vscode');
  assert.equal(
    pipUpgradeCommand('/home/me/.venv/bin/python'),
    '/home/me/.venv/bin/python -m pip install -U langstage-vscode',
  );
  assert.equal(
    pipUpgradeCommand('/opt/my tools/bin/python', 'linux'),
    '"/opt/my tools/bin/python" -m pip install -U langstage-vscode',
  );
  // Windows: PowerShell (VS Code's default terminal there) needs `&` to run a quoted path.
  assert.equal(
    pipUpgradeCommand('C:\\Program Files\\Python312\\python.exe', 'win32'),
    '& "C:\\Program Files\\Python312\\python.exe" -m pip install -U langstage-vscode',
  );
  assert.equal(pipUpgradeCommand('python', 'win32'), 'python -m pip install -U langstage-vscode');
});

test('outdatedNotice names the interpreter, the version (or its absence) and the command', () => {
  const withVersion = outdatedNotice('/venv/bin/python', '0.5.20');
  assert.equal(withVersion.python, '/venv/bin/python');
  assert.equal(withVersion.command, '/venv/bin/python -m pip install -U langstage-vscode');
  assert.equal(
    withVersion.message,
    'LangStage: the langstage-vscode sidecar in /venv/bin/python is 0.5.20; this extension ' +
      'expects 0.5.35 or newer, so some features may not work. Update it with: ' +
      '/venv/bin/python -m pip install -U langstage-vscode',
  );
  const noVersion = outdatedNotice('python', undefined);
  assert.match(noVersion.message, /sidecar in python is older than 0\.5\.35; this extension expects 0\.5\.35 or newer/);
  assert.match(noVersion.message, /Update it with: python -m pip install -U langstage-vscode$/);
});

test('warns once per interpreter, and only for an outdated sidecar', () => {
  const shown: OutdatedNotice[] = [];
  const warnings = new OutdatedSidecarWarnings((n) => shown.push(n));

  assert.equal(warnings.check('python', CURRENT), false, 'a current sidecar: no warning');
  assert.equal(warnings.check('python', { version: '0.6.0' }), false);
  assert.equal(warnings.check('python', { version: '0.9.0', protocol: 2 }), false, 'refused, not warned');
  assert.equal(shown.length, 0);

  assert.equal(warnings.check('python', {}), true, 'no version: warn');
  assert.equal(warnings.check('python', {}), false, 'but only once');
  assert.equal(warnings.check('python', { version: '0.5.20' }), false, 'once per interpreter, whatever it reports');
  assert.equal(warnings.check('/venv/bin/python', { version: '0.5.20' }), true, 'another interpreter is warned too');
  assert.equal(warnings.check('/venv/bin/python', { version: '0.5.20' }), false);
  assert.deepEqual(
    shown.map((n) => [n.python, n.message.includes('older than 0.5.35'), n.message.includes(' is 0.5.20;')]),
    [
      ['python', true, false],
      ['/venv/bin/python', false, true],
    ],
  );

  warnings.reset();
  assert.equal(warnings.check('python', {}), true, 'a new activation warns again');
  assert.equal(shown.length, 3);
});

test('several sidecars on one interpreter (the panel and @langstage, restarts) warn once', async () => {
  const shown: OutdatedNotice[] = [];
  const warnings = new OutdatedSidecarWarnings((n) => shown.push(n));
  const { spawn, procs } = fakeSpawn();
  const make = () =>
    new SidecarClient({
      python: 'python',
      args: [],
      cwd: '.',
      spawn,
      exitGraceMs: 20,
      onReady: (info) => warnings.check('python', info),
    });
  const clients = [make(), make(), make()];
  for (const [i, c] of clients.entries()) {
    const ready = c.start();
    await tick();
    procs[i].emitFrames([{ type: 'ready' }]); // an older sidecar: no handshake
    await ready;
  }
  assert.equal(shown.length, 1);
  assert.equal(shown[0].command, 'python -m pip install -U langstage-vscode');
  for (const c of clients) c.dispose();
});
