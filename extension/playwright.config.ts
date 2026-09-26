// Playwright: the panel's webview harness against the real sidecar (build plan M5).
//   npm run compile && npm run test:e2e
// Needs a Python with langstage-vscode installed: LANGSTAGE_PYTHON=<path or command>.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'test/e2e',
  outputDir: 'test-results',
  timeout: 90_000,
  expect: { timeout: 30_000 },
  fullyParallel: true,
  // Each test runs its own sidecar and Chromium (with video). Parallel runs starved the
  // Python startup on a Windows dev box (about a minute to `ready`), so serial locally.
  workers: process.env.CI ? 2 : 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    browserName: 'chromium',
    // About the width of a sidebar view.
    viewport: { width: 440, height: 820 },
    video: 'on',
    trace: 'retain-on-failure',
  },
});
