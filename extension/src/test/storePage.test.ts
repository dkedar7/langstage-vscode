// The store page (gh #160): the VSIX's README and CHANGELOG are what the Marketplace, Open
// VSX and the Extensions view show. Keep them in step with package.json. `npm test`.
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'fs';

interface Manifest {
  version: string;
  contributes: {
    configuration: { properties: Record<string, { deprecationMessage?: string }> };
    commands: Array<{ command: string; title: string; category?: string }>;
    menus: { commandPalette: Array<{ command: string; when: string }> };
  };
}

const manifest = JSON.parse(fs.readFileSync('package.json', 'utf8')) as Manifest;
const readme = fs.readFileSync('README.md', 'utf8');
const changelog = fs.readFileSync('CHANGELOG.md', 'utf8');

test('the store README gives the two-part install and the demo', () => {
  assert.match(readme, /pip install langstage-vscode/);
  assert.match(readme, /Try the demo/);
  assert.match(readme, /https:\/\/dkedar7\.github\.io\/langstage-docs/);
});

test('the store README documents every setting and palette command', () => {
  const { properties } = manifest.contributes.configuration;
  for (const [key, prop] of Object.entries(properties)) {
    if (prop.deprecationMessage) continue;
    assert.ok(readme.includes(`\`${key}\``), `README is missing the ${key} setting`);
  }
  const hidden = new Set(
    manifest.contributes.menus.commandPalette.filter((m) => m.when === 'false').map((m) => m.command),
  );
  for (const cmd of manifest.contributes.commands) {
    if (hidden.has(cmd.command)) continue;
    const label = cmd.category ? `${cmd.category}: ${cmd.title}` : cmd.title;
    assert.ok(readme.includes(label), `README is missing the "${label}" command`);
  }
});

test('the store README uses only absolute links and images', () => {
  // A relative link resolves against the store page, not the repo, so it breaks there.
  const targets = [
    ...[...readme.matchAll(/\]\(([^)\s]+)/g)].map((m) => m[1]),
    ...[...readme.matchAll(/\bsrc="([^"]+)"/g)].map((m) => m[1]),
  ];
  assert.ok(targets.length > 0);
  for (const t of targets) assert.match(t, /^https:\/\//, `relative link in the store README: ${t}`);
});

test('the store CHANGELOG has an entry for this version', () => {
  const heading = `## [${manifest.version}] - `;
  assert.ok(changelog.includes(heading), `extension/CHANGELOG.md has no "${heading}" entry`);
});
