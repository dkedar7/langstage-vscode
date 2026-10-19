// Legacy `deepagent.*` settings still apply when the `langstage.*` one isn't set (gh #168).
// No vscode needed: the inspect() layers are plain objects. `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import { explicitValue, withLegacyFallback } from '../launchSettings';

// What inspect('pythonPath') returns when only the contributed default exists.
const DEFAULT_ONLY = { key: 'langstage.pythonPath', defaultValue: 'python' };

test('a legacy deepagent.pythonPath beats the contributed langstage default (gh #168)', () => {
  const legacy = { key: 'deepagent.pythonPath', workspaceValue: '/venv/bin/python' };
  assert.equal(withLegacyFallback<string>(DEFAULT_ONLY, legacy, 'python'), '/venv/bin/python');
});

test('a langstage.pythonPath someone set beats the legacy one', () => {
  const current = { ...DEFAULT_ONLY, globalValue: '/new/python' };
  const legacy = { workspaceValue: '/old/python' };
  assert.equal(withLegacyFallback<string>(current, legacy, 'python'), '/new/python');
});

test('nothing set anywhere falls back to plain python', () => {
  assert.equal(withLegacyFallback<string>(DEFAULT_ONLY, undefined, 'python'), 'python');
  assert.equal(withLegacyFallback<string>(DEFAULT_ONLY, { key: 'deepagent.pythonPath' }, 'python'), 'python');
});

test('an empty string counts as unset', () => {
  const current = { ...DEFAULT_ONLY, workspaceValue: '' };
  assert.equal(withLegacyFallback<string>(current, { globalValue: '/old/python' }, 'python'), '/old/python');
});

test('the most specific layer wins: folder, then workspace, then user', () => {
  assert.equal(explicitValue({ globalValue: 'u', workspaceValue: 'w', workspaceFolderValue: 'f' }), 'f');
  assert.equal(explicitValue({ globalValue: 'u', workspaceValue: 'w' }), 'w');
  assert.equal(explicitValue({ globalValue: 'u' }), 'u');
  assert.equal(explicitValue(undefined), undefined);
});
