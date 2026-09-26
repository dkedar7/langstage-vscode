// Bundles the LangStage panel's webview (React UI) into dist/webview/{main.js,main.css}.
// The extension host itself is still compiled by `tsc` (it has no runtime dependencies).
//
//   node esbuild.mjs            production bundle (minified)
//   node esbuild.mjs --watch    rebuild on change
//   node esbuild.mjs --tests    bundle webview unit tests to dist/test/webview/
import * as esbuild from 'esbuild';
import { readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const args = new Set(process.argv.slice(2));
const BUDGET = 400 * 1024; // build plan M1: webview under 400 KB minified

if (args.has('--tests')) {
  const dir = 'webview/test';
  const entryPoints = readdirSync(dir).filter((f) => f.endsWith('.test.ts')).map((f) => join(dir, f));
  await esbuild.build({
    entryPoints,
    bundle: true,
    platform: 'node',
    format: 'cjs',
    target: 'node18',
    outdir: 'dist/test/webview',
    logLevel: 'warning',
  });
  process.exit(0);
}

// ADR 0001 open question 6 (React vs Preact), measured in M1 with this UI: React 19 is
// 387 KB minified, Preact 10 via preact/compat 195 KB. The source is written against
// React's API and types (a faithful port of the web frontend); only the bundle swaps in
// preact/compat. `--react` builds with React itself, for comparison or as a fallback.
const alias = args.has('--react')
  ? {}
  : {
      react: 'preact/compat',
      'react-dom': 'preact/compat',
      'react-dom/client': 'preact/compat/client',
      'react/jsx-runtime': 'preact/jsx-runtime',
      'react/jsx-dev-runtime': 'preact/jsx-runtime',
    };

const options = {
  entryPoints: { main: 'webview/main.tsx' },
  alias,
  bundle: true,
  format: 'iife',
  platform: 'browser',
  target: ['chrome114'],
  minify: !args.has('--watch'),
  sourcemap: args.has('--watch') ? 'inline' : false,
  outdir: 'dist/webview',
  jsx: 'automatic',
  define: { 'process.env.NODE_ENV': args.has('--watch') ? '"development"' : '"production"' },
  legalComments: 'none',
  logLevel: 'warning',
};

if (args.has('--watch')) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  console.log('watching webview/ …');
} else {
  await esbuild.build(options);
  const size = statSync('dist/webview/main.js').size;
  const kb = (size / 1024).toFixed(1);
  console.log(`dist/webview/main.js ${kb} KB (budget ${BUDGET / 1024} KB)`);
  if (size > BUDGET) {
    console.error('webview bundle is over budget');
    process.exit(1);
  }
}
