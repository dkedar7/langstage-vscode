// The panel demo recording (build plan M5): `npm run record`.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '.',
  testMatch: /record-demo\.ts$/,
  outputDir: '../../test-results/record',
  timeout: 180_000,
  expect: { timeout: 60_000 },
  workers: 1,
  reporter: 'list',
  use: { browserName: 'chromium' },
});
