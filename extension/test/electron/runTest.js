// Editor smoke test (build plan M5): the built extension in a real VS Code, downloaded
// by @vscode/test-electron. No Copilot, no API key.
//
//   npm run compile && npm run test:smoke          (Linux CI: xvfb-run -a npm run test:smoke)
//
// The sidecar round trip needs a Python with langstage-vscode installed:
// LANGSTAGE_PYTHON=<path or command> (default `python`).
'use strict';
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { runTests } = require('@vscode/test-electron');

async function main() {
  const extensionDevelopmentPath = path.resolve(__dirname, '..', '..');
  const extensionTestsPath = path.resolve(__dirname, 'suite', 'index.js');

  // A folder workspace (so the panel gets workspace storage), pointed at the Python.
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'ls-smoke-'));
  const envPython = process.env.LANGSTAGE_PYTHON || 'python';
  const python = /[\\/]/.test(envPython) ? path.resolve(envPython) : envPython;
  fs.mkdirSync(path.join(workspace, '.vscode'));
  fs.writeFileSync(
    path.join(workspace, '.vscode', 'settings.json'),
    JSON.stringify({ 'langstage.pythonPath': python }, null, 2),
  );

  await runTests({
    version: process.env.VSCODE_TEST_VERSION || 'stable',
    extensionDevelopmentPath,
    extensionTestsPath,
    launchArgs: [
      workspace,
      '--disable-extensions', // no Copilot, nothing else: only this extension
      '--disable-workspace-trust', // the extension is disabled in Restricted Mode
      '--skip-welcome',
      '--skip-release-notes',
    ],
  });
}

main().catch((err) => {
  console.error('editor smoke test failed:', err);
  process.exit(1);
});
