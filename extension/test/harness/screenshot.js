// Render the panel's real webview bundle in Chromium from a recorded `restore` and save
// a screenshot. Test-only (Playwright is not an extension dependency):
//
//   npm run compile
//   LANGSTAGE_PYTHON=... node test/real-sidecar-check.js --out restore.json
//   NODE_PATH=<a node_modules with playwright> node test/harness/screenshot.js restore.json out.png [--todos]
//
// --todos appends a `write_todos` tool call and its `todos` extraction to the recorded
// transcript (the keyless demo agent has no planning tool), so the checklist shows.
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { chromium } = require('playwright');

const [restoreFile, outFile] = process.argv.slice(2).filter((a) => !a.startsWith('--'));
const withTodos = process.argv.includes('--todos');

const restore = JSON.parse(fs.readFileSync(restoreFile, 'utf8'));
if (withTodos) {
  const log = restore.transcripts[restore.activeId];
  const todos = [
    { content: 'Look up the answer with demo_lookup', status: 'completed' },
    { content: 'Reason about the result', status: 'completed' },
    { content: 'Summarize for the user', status: 'in_progress' },
    { content: 'Ask before changing anything', status: 'pending' },
  ];
  log.push(
    { kind: 'frame', frame: { type: 'tool_start', id: 'todos_1', name: 'write_todos', args: { todos } } },
    { kind: 'frame', frame: { type: 'tool_end', id: 'todos_1', name: 'write_todos', result: 'Updated todo list', status: 'success', duration_ms: 4 } },
    { kind: 'frame', frame: { type: 'extraction', tool_name: 'write_todos', extracted_type: 'todos', data: todos } },
  );
}
const harness = __dirname;
fs.writeFileSync(path.join(harness, 'restore.js'), `window.__RESTORE__ = ${JSON.stringify(restore)};\n`);

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 420, height: 1180 }, deviceScaleFactor: 2 });
  const errors = [];
  page.on('pageerror', (e) => errors.push(String(e)));
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
  await page.goto(pathToFileURL(path.join(harness, 'index.html')).href);
  await page.waitForSelector('.ls-tool');
  // Open the first tool card and the reasoning block, as a reviewer would.
  await page.locator('.ls-tool-head').first().click();
  await page.locator('.ls-reasoning-head').first().click();
  await page.locator('textarea').fill('Summarize what the tool found');
  const counts = await page.evaluate(() => ({
    tools: document.querySelectorAll('.ls-tool').length,
    reasoning: document.querySelectorAll('.ls-reasoning').length,
    assistant: document.querySelectorAll('.ls-msg-assistant').length,
    todos: document.querySelectorAll('.ls-todo').length,
    sent: window.__sent.map((m) => m.type),
  }));
  console.log(JSON.stringify(counts));
  await page.screenshot({ path: outFile, fullPage: false });
  await browser.close();
  fs.unlinkSync(path.join(harness, 'restore.js'));
  if (errors.length) {
    console.error('page errors:', errors);
    process.exit(1);
  }
  console.log(`wrote ${outFile}`);
})();
