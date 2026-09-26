// Records the LangStage panel demo: docs/assets/panel-demo.webm and panel-demo.gif.
//
//   npm run compile && LANGSTAGE_PYTHON=<python> npm run record
//
// What it records is the real webview bundle in the webview harness (test/harness/,
// themed with VS Code's Dark Modern colors), driven through the real host logic against
// the real sidecar running the keyless `--demo=tools` agent: send a message → a tool
// card → reasoning → an approval prompt → Approve → the resumed reply. No Copilot, no
// API key. It is the harness page, not a full editor window (a code-server recording is
// the follow-up the build plan describes).
//
// The GIF needs ffmpeg on PATH (or $FFMPEG).
import { execFileSync } from 'child_process';
import * as fs from 'fs';
import * as path from 'path';
import { expect, test, type Page } from '@playwright/test';
import { Relay } from '../harness/relay';

const ASSETS = path.resolve(__dirname, '..', '..', '..', 'docs', 'assets');
const SIZE = { width: 440, height: 720 };

async function type(page: Page, text: string) {
  const box = page.getByLabel('Message');
  await box.click();
  await box.pressSequentially(text, { delay: 55 });
  await page.waitForTimeout(350);
  await box.press('Enter');
}

test('record the panel demo', async ({ browser }) => {
  const videoDir = test.info().outputPath('video');
  const context = await browser.newContext({
    viewport: SIZE,
    deviceScaleFactor: 1,
    recordVideo: { dir: videoDir, size: SIZE },
  });
  const page = await context.newPage();
  const relay = new Relay(page, { agent: 'demo' });
  try {
    await relay.open();
    await expect(page.getByRole('status').filter({ hasText: 'Ready' })).toBeVisible({ timeout: 120_000 });
    await page.waitForTimeout(900);

    // 1. A message → a tool card and a streamed reply.
    await type(page, 'please use a tool');
    await expect(page.locator('.ls-msg-assistant').last()).toContainText('extraction flow');
    await page.waitForTimeout(700);
    await page.locator('.ls-tool-head').first().click();
    await page.waitForTimeout(2200);
    await page.locator('.ls-tool-head').first().click();
    await page.waitForTimeout(400);

    // 2. Reasoning, streamed into its own block.
    await type(page, 'think about it');
    await expect(page.locator('.ls-msg-assistant').last()).toContainText('Done reasoning');
    await page.waitForTimeout(500);
    await page.locator('.ls-reasoning-head').last().click();
    await page.waitForTimeout(2000);

    // 3. An approval prompt → Approve → the resumed reply.
    await type(page, 'ask me');
    const card = page.getByRole('group', { name: 'Approval request' }).last();
    await expect(card.getByRole('button', { name: 'Approve' })).toBeVisible();
    await page.waitForTimeout(2200);
    await card.getByRole('button', { name: 'Approve' }).hover();
    await page.waitForTimeout(600);
    await card.getByRole('button', { name: 'Approve' }).click();
    await expect(page.locator('.ls-msg-assistant').last()).toContainText('Resumed');
    await page.waitForTimeout(2500);
  } finally {
    relay.close();
    await context.close();
  }

  const video = await page.video()!.path();
  fs.mkdirSync(ASSETS, { recursive: true });
  const webm = path.join(ASSETS, 'panel-demo.webm');
  const gif = path.join(ASSETS, 'panel-demo.gif');
  fs.copyFileSync(video, webm);
  const ffmpeg = process.env.FFMPEG || 'ffmpeg';
  // Two-pass palette GIF: 10 fps at the recorded width, small enough for a README.
  const filters = 'fps=10,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle';
  execFileSync(ffmpeg, ['-y', '-loglevel', 'error', '-i', webm, '-filter_complex', filters, '-loop', '0', gif], {
    stdio: 'inherit',
  });
  const kb = (f: string) => `${(fs.statSync(f).size / 1024).toFixed(0)} KB`;
  console.log(`wrote ${webm} (${kb(webm)}) and ${gif} (${kb(gif)})`);
});
