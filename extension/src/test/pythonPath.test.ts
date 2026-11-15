// Choosing the sidecar's interpreter (gh #178): variables in langstage.pythonPath, the
// workspace venv when nothing is set, and a clear refusal for what can't be expanded.
// No vscode and no real filesystem: `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { PythonInputs, expandVariables, interpreterNotFoundMessage, resolvePython } from '../pythonPath';

function posix(extra: Partial<PythonInputs> = {}, files: string[] = []): PythonInputs {
  return {
    env: {},
    folders: [{ name: 'app', path: '/work/app' }],
    home: '/home/me',
    platform: 'linux',
    exists: (p) => files.includes(p),
    ...extra,
  };
}

function windows(extra: Partial<PythonInputs> = {}, files: string[] = []): PythonInputs {
  return {
    env: {},
    folders: [{ name: 'app', path: 'C:\\work\\app' }],
    home: 'C:\\Users\\me',
    platform: 'win32',
    exists: (p) => files.includes(p),
    ...extra,
  };
}

test('an absolute path with nothing to expand is passed through untouched', () => {
  const p = resolvePython(posix({ setting: '/opt/py/bin/python3' }));
  assert.deepEqual(p, { python: '/opt/py/bin/python3', source: 'setting', how: 'langstage.pythonPath' });
  const w = resolvePython(windows({ setting: 'C:\\Py312\\python.exe' }));
  assert.equal(w.python, 'C:\\Py312\\python.exe');
  // Forward slashes someone typed on Windows are left as typed.
  assert.equal(resolvePython(windows({ setting: 'C:/Py312/python.exe' })).python, 'C:/Py312/python.exe');
});

test('a bare command stays a command looked up on PATH', () => {
  assert.equal(resolvePython(posix({ setting: 'python3' })).python, 'python3');
  assert.equal(resolvePython(windows({ setting: 'py' })).python, 'py');
});

test('${workspaceFolder} is the first folder: the committed-settings example', () => {
  const p = resolvePython(posix({ setting: '${workspaceFolder}/.venv/bin/python' }));
  assert.equal(p.python, '/work/app/.venv/bin/python');
  assert.equal(p.error, undefined);
  const w = resolvePython(windows({ setting: '${workspaceFolder}/.venv/Scripts/python.exe' }));
  assert.equal(w.python, 'C:\\work\\app\\.venv\\Scripts\\python.exe');
});

test('${workspaceFolder:<name>} picks a folder of a multi-root workspace by name', () => {
  const folders = [
    { name: 'web', path: '/work/web' },
    { name: 'agent', path: '/work/agent' },
  ];
  const p = resolvePython(posix({ folders, setting: '${workspaceFolder:agent}/.venv/bin/python' }));
  assert.equal(p.python, '/work/agent/.venv/bin/python');
  const missing = resolvePython(posix({ folders, setting: '${workspaceFolder:api}/.venv/bin/python' }));
  assert.match(missing.error ?? '', /no workspace folder is named "api" \(open: web, agent\)/);
});

test('a leading ~ and ${userHome} are the home directory; ~ elsewhere is literal', () => {
  assert.equal(resolvePython(posix({ setting: '~/venvs/agent/bin/python' })).python, '/home/me/venvs/agent/bin/python');
  assert.equal(resolvePython(posix({ setting: '${userHome}/v/bin/python' })).python, '/home/me/v/bin/python');
  assert.equal(
    resolvePython(windows({ setting: '~\\venvs\\a\\Scripts\\python.exe' })).python,
    'C:\\Users\\me\\venvs\\a\\Scripts\\python.exe',
  );
  assert.equal(resolvePython(posix({ setting: '/opt/~x/python' })).python, '/opt/~x/python');
  // `~user` is not expanded (VS Code doesn't either): it stays a relative path.
  assert.equal(resolvePython(posix({ setting: '~bob/bin/python' })).python, '/work/app/~bob/bin/python');
});

test('${env:NAME} reads the environment; an unset one is empty, as in launch.json', () => {
  const env = { VENVS: '/srv/venvs' };
  assert.equal(
    resolvePython(posix({ env, setting: '${env:VENVS}/agent/bin/python' })).python,
    '/srv/venvs/agent/bin/python',
  );
  assert.equal(resolvePython(posix({ setting: '${env:NOPE}/usr/bin/python3' })).python, '/usr/bin/python3');
  const empty = resolvePython(posix({ setting: '${env:NOPE}' }));
  assert.match(empty.error ?? '', /expands to nothing/);
});

test('a relative path is resolved against the workspace folder on every OS', () => {
  assert.equal(resolvePython(posix({ setting: '.venv/bin/python' })).python, '/work/app/.venv/bin/python');
  assert.equal(
    resolvePython(windows({ setting: '.venv\\Scripts\\python.exe' })).python,
    'C:\\work\\app\\.venv\\Scripts\\python.exe',
  );
  assert.equal(
    resolvePython(windows({ setting: '.venv/Scripts/python.exe' })).python,
    'C:\\work\\app\\.venv\\Scripts\\python.exe',
  );
  // No folder open: nothing to resolve against, so it is left for spawn to report.
  assert.equal(resolvePython(posix({ folders: [], setting: '.venv/bin/python' })).python, '.venv/bin/python');
});

test("a venv's folder stands for its interpreter, so one value works on every OS", () => {
  const p = resolvePython(posix({ setting: '${workspaceFolder}/.venv' }, ['/work/app/.venv/bin/python']));
  assert.equal(p.python, '/work/app/.venv/bin/python');
  const w = resolvePython(
    windows({ setting: '${workspaceFolder}/.venv' }, ['C:\\work\\app\\.venv\\Scripts\\python.exe']),
  );
  assert.equal(w.python, 'C:\\work\\app\\.venv\\Scripts\\python.exe');
  // Not a venv (no interpreter inside): left as it expanded, for spawn to report.
  assert.equal(resolvePython(posix({ setting: '${workspaceFolder}/.venv' })).python, '/work/app/.venv');
  // A bare command is never probed as a folder.
  const probed: string[] = [];
  resolvePython(posix({ setting: 'python', exists: (x) => (probed.push(x), false) }));
  assert.deepEqual(probed, []);
});

test('an unknown ${...} is refused by name, with the supported list', () => {
  const p = resolvePython(posix({ setting: '${workspaceRoot}/.venv/bin/python' }));
  assert.match(p.error ?? '', /langstage\.pythonPath uses \$\{workspaceRoot\}, which isn't supported here/);
  assert.match(p.error ?? '', /\$\{workspaceFolder\}/);
  const cmd = resolvePython(posix({ setting: '${command:python.interpreterPath}' }));
  assert.match(cmd.error ?? '', /\$\{command:python\.interpreterPath\}/);
});

test('${workspaceFolder} with no folder open is an error, not a literal path', () => {
  const p = resolvePython(posix({ folders: [], setting: '${workspaceFolder}/.venv/bin/python' }));
  assert.match(p.error ?? '', /no folder is open/);
});

test('order: langstage.pythonPath, deepagent.pythonPath, LANGSTAGE_PYTHON, venv, PATH', () => {
  const files = ['/work/app/.venv/bin/python'];
  const env = { LANGSTAGE_PYTHON: '/env/python' };
  const all = posix({ setting: '/s/python', legacySetting: '/l/python', env }, files);
  assert.equal(resolvePython(all).source, 'setting');
  const legacy = resolvePython({ ...all, setting: undefined });
  assert.deepEqual([legacy.python, legacy.source, legacy.how], ['/l/python', 'legacy-setting', 'deepagent.pythonPath']);
  const fromEnv = resolvePython({ ...all, setting: undefined, legacySetting: undefined });
  assert.deepEqual([fromEnv.python, fromEnv.source, fromEnv.how], ['/env/python', 'env', 'LANGSTAGE_PYTHON']);
  const venv = resolvePython(posix({}, files));
  assert.deepEqual(venv, { python: '/work/app/.venv/bin/python', source: 'venv', how: '.venv (auto-detected)' });
  assert.deepEqual(resolvePython(posix()), { python: 'python', source: 'path', how: 'python on PATH' });
  // An empty string counts as unset, as before (gh #168).
  assert.equal(resolvePython(posix({ setting: '' }, files)).source, 'venv');
});

test('the workspace venv: .venv before venv, the platform layout only, first folder only', () => {
  const both = ['/work/app/.venv/bin/python', '/work/app/venv/bin/python'];
  assert.equal(resolvePython(posix({}, both)).how, '.venv (auto-detected)');
  const plain = resolvePython(posix({}, ['/work/app/venv/bin/python']));
  assert.deepEqual([plain.python, plain.how], ['/work/app/venv/bin/python', 'venv (auto-detected)']);
  const win = resolvePython(windows({}, ['C:\\work\\app\\.venv\\Scripts\\python.exe']));
  assert.deepEqual([win.python, win.source], ['C:\\work\\app\\.venv\\Scripts\\python.exe', 'venv']);
  // A venv made on the other OS (WSL's bin/python seen from Windows) can't run here.
  assert.equal(resolvePython(windows({}, ['C:\\work\\app\\.venv\\bin\\python'])).source, 'path');
  // Only the first folder: it is the sidecar's working directory.
  const folders = [
    { name: 'web', path: '/work/web' },
    { name: 'agent', path: '/work/agent' },
  ];
  assert.equal(resolvePython(posix({ folders }, ['/work/agent/.venv/bin/python'])).source, 'path');
  // No folder open: nothing to look in.
  assert.equal(resolvePython(posix({ folders: [] }, both)).source, 'path');
});

test('LANGSTAGE_PYTHON and the legacy setting expand the same variables', () => {
  const env = { LANGSTAGE_PYTHON: '~/v/bin/python' };
  assert.equal(resolvePython(posix({ env })).python, '/home/me/v/bin/python');
  const legacy = resolvePython(posix({ legacySetting: '${nope}' }));
  assert.match(legacy.error ?? '', /^deepagent\.pythonPath uses \$\{nope\}/);
});

test('expandVariables reports whether anything was substituted', () => {
  const same = expandVariables('/usr/bin/python3', posix(), 'x');
  assert.deepEqual(same, { ok: true, value: '/usr/bin/python3', changed: false });
  const changed = expandVariables('${userHome}/p', posix(), 'x');
  assert.deepEqual(changed, { ok: true, value: '/home/me/p', changed: true });
});

test('the not-found message names the interpreter, where it came from and the ways out', () => {
  const m = interpreterNotFoundMessage('/work/app/.venv/bin/python', 'langstage.pythonPath');
  assert.match(m, /^Python interpreter not found: \/work\/app\/\.venv\/bin\/python \(from langstage\.pythonPath\)\./);
  assert.match(m, /leave it empty to use the workspace's \.venv/);
  assert.match(interpreterNotFoundMessage('python', undefined), /^Python interpreter not found: python\. /);
});
