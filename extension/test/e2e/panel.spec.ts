// The LangStage panel end to end, with no Copilot and no API key (build plan M5): the
// real webview bundle in Chromium, the real host logic, the real sidecar. Most tests run
// the keyless `--demo=tools` agent; reject / edit / Stop use test/fixtures/hitl_agent.py
// (the demo's interrupt allows only respond + approve, and its turns are too quick to
// stop).
import { expect, test, type Page } from '@playwright/test';
import { FIXTURE_AGENT, Relay } from '../harness/relay';

let relay: Relay | undefined;

test.afterEach(() => {
  relay?.close();
  relay = undefined;
});

async function openPanel(page: Page, agent: string, storeDir?: string): Promise<Relay> {
  relay = new Relay(page, { agent, storeDir });
  await relay.open();
  await expect(page.getByRole('status').filter({ hasText: 'Ready' })).toBeVisible({ timeout: 90_000 });
  return relay;
}

async function send(page: Page, text: string) {
  await page.getByLabel('Message').fill(text);
  await page.getByLabel('Message').press('Enter');
}

const card = (page: Page) => page.getByRole('group', { name: 'Approval request' }).last();
const lastReply = (page: Page) => page.locator('.ls-msg-assistant').last();
const idle = (page: Page) => expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();

test('streams a reply and pairs the tool card', async ({ page }) => {
  const r = await openPanel(page, 'demo');
  await expect(page.getByRole('status')).toContainText('demo agent (--demo=tools)');
  await send(page, 'please use a tool');
  const tool = page.locator('.ls-tool');
  await expect(tool.locator('.ls-tool-name')).toHaveText('demo_lookup');
  await expect(tool.getByLabel('succeeded')).toBeVisible();
  await expect(lastReply(page)).toContainText('The demo tool returned');
  await idle(page);
  // It streamed: many content frames reached the webview, joined into one block.
  expect(r.frames('content').length).toBeGreaterThan(5);
  await expect(page.locator('.ls-msg-assistant')).toHaveCount(1);
  // The card opens to its arguments, result and extraction.
  await tool.locator('.ls-tool-head').click();
  await expect(tool).toContainText('"query": "please use a tool"');
  await expect(tool).toContainText('Extracted · demo_fact');
});

test('reasoning streams into its own collapsed block', async ({ page }) => {
  await openPanel(page, 'demo');
  await send(page, 'think about it');
  await expect(lastReply(page)).toContainText('Done reasoning');
  const reasoning = page.locator('.ls-reasoning');
  await expect(reasoning).toHaveCount(1);
  await expect(reasoning.locator('.ls-reasoning-body')).toHaveCount(0);
  await reasoning.locator('.ls-reasoning-head').click();
  await expect(reasoning.locator('.ls-reasoning-body')).toContainText('step by step');
});

test('approval card: the allowed verbs only; Approve resumes the agent', async ({ page }) => {
  await openPanel(page, 'demo');
  await send(page, 'ask me');
  const c = card(page);
  await expect(c).toContainText('The agent is waiting for your decision');
  await expect(c).toContainText('ask_user');
  await expect(c).toContainText('What should I call you?');
  // --demo=tools allows respond + approve: no Reject or Edit button.
  await expect(c.getByRole('button')).toHaveText(['Approve', 'Respond…']);
  // While paused, the composer says so and won't send.
  await expect(page.getByText('Answer the request above to continue.')).toBeVisible();
  await page.getByLabel('Message').fill('hello?');
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();

  await c.getByRole('button', { name: 'Approve' }).click();
  await expect(c).toContainText('Approved');
  await expect(lastReply(page)).toContainText('Resumed. Your decision was');
  await expect(c.getByRole('button')).toHaveCount(0);
  await expect(page.getByText('Answer the request above to continue.')).toHaveCount(0);
});

test('approval card: Respond answers in words', async ({ page }) => {
  await openPanel(page, 'demo');
  await send(page, 'ask me');
  const c = card(page);
  await c.getByRole('button', { name: 'Respond…' }).click();
  await c.getByLabel('Your response').fill('Call me Kedar');
  await c.getByRole('button', { name: 'Send response' }).click();
  await expect(c).toContainText('Responded: Call me Kedar');
  await expect(lastReply(page)).toContainText('Call me Kedar');
});

test('a refused decision keeps the card live and shows the sidecar error', async ({ page }) => {
  const r = await openPanel(page, 'demo');
  await send(page, 'ask me');
  const c = card(page);
  await expect(c.getByRole('button', { name: 'Approve' })).toBeVisible();
  // A decision the interrupt doesn't allow (the UI never offers it; a stale card or
  // another client could): the sidecar refuses it and the interrupt stays pending.
  r.toHost({ type: 'decide', conversationId: r.activeId(), decisions: [{ type: 'reject' }] });
  await expect(c).toContainText('The agent refused this answer');
  await expect(c).toContainText("decision 'reject' is not allowed");
  await c.getByRole('button', { name: 'Approve' }).click();
  await expect(lastReply(page)).toContainText('Resumed. Your decision was');
  await expect(c).not.toContainText('refused');
});

test('approval card: Reject with a reason', async ({ page }) => {
  await openPanel(page, FIXTURE_AGENT);
  await send(page, 'write a file');
  const c = card(page);
  await expect(c.getByRole('button')).toHaveText(['Approve', 'Edit…', 'Respond…', 'Reject…']);
  await c.getByRole('button', { name: 'Reject…' }).click();
  await c.getByLabel('Reason (optional)').fill('not in this folder');
  await c.getByRole('button', { name: 'Reject', exact: true }).click();
  await expect(c).toContainText('Rejected: not in this folder');
  await expect(lastReply(page)).toContainText('"message": "not in this folder", "type": "reject"');
});

test('approval card: Edit the arguments as JSON', async ({ page }) => {
  await openPanel(page, FIXTURE_AGENT);
  await send(page, 'write a file');
  const c = card(page);
  await c.getByRole('button', { name: 'Edit…' }).click();
  const editor = c.getByLabel('Arguments for write_file (JSON)');
  await expect(editor).toHaveValue(/"path": "notes.txt"/);
  await editor.fill('{"path": "edited.txt",');
  await c.getByRole('button', { name: 'Approve with edits' }).click();
  await expect(c.getByRole('alert')).toContainText('not valid JSON');
  await editor.fill('{"path": "edited.txt", "content": "changed"}');
  await c.getByRole('button', { name: 'Approve with edits' }).click();
  await expect(c).toContainText('Approved with edited arguments');
  await expect(lastReply(page)).toContainText('"args": {"content": "changed", "path": "edited.txt"}');
  await expect(lastReply(page)).toContainText('"type": "edit"');
});

test('Stop cancels mid-turn and the conversation carries on', async ({ page }) => {
  const r = await openPanel(page, FIXTURE_AGENT);
  await send(page, 'count slowly');
  await expect(lastReply(page)).toContainText('3');
  await page.getByRole('button', { name: 'Stop' }).click();
  await expect(page.locator('.ls-notice', { hasText: 'Stopped.' })).toBeVisible();
  await idle(page);
  expect(r.frames('cancelled').length).toBe(1);
  await expect(lastReply(page)).not.toContainText('60');
  await send(page, 'still there?');
  await expect(lastReply(page)).toContainText('You said: still there?');
});

test('a second conversation queues behind a running turn; Stop in one leaves the other', async ({ page }) => {
  await openPanel(page, FIXTURE_AGENT);
  await send(page, 'count slowly');
  await expect(lastReply(page)).toContainText('2');
  await page.getByRole('button', { name: 'New conversation' }).click();
  await expect(page.locator('.ls-msg')).toHaveCount(0);
  await send(page, 'hello from B');
  await expect(page.getByText('Queued behind another turn…')).toBeVisible();

  // The list shows both: B queued, A running.
  await page.getByRole('button', { name: 'Conversations' }).click();
  const list = page.getByRole('list', { name: 'Conversation list' });
  await expect(list.getByRole('listitem')).toHaveCount(2);
  await expect(list.getByRole('listitem').first()).toContainText('hello from B');
  await expect(list.getByRole('listitem').first()).toContainText('queued');
  // Back to A and stop it: B then runs.
  await list.getByRole('button', { name: 'count slowly', exact: true }).click();
  await expect(page.locator('.ls-msg-user .ls-msg-body')).toHaveText(['count slowly']);
  await page.getByRole('button', { name: 'Stop' }).click();
  await expect(page.locator('.ls-notice', { hasText: 'Stopped.' })).toBeVisible();
  await page.getByRole('button', { name: 'Conversations' }).click();
  await list.getByRole('button', { name: 'hello from B', exact: true }).click();
  await expect(lastReply(page)).toContainText('You said: hello from B');
});

test('conversations: rename and delete', async ({ page }) => {
  await openPanel(page, 'demo');
  await send(page, 'first chat');
  await idle(page);
  await page.getByRole('button', { name: 'New conversation' }).click();
  await send(page, 'second chat');
  await idle(page);
  await page.getByRole('button', { name: 'Conversations' }).click();
  const list = page.getByRole('list', { name: 'Conversation list' });
  await list.getByRole('button', { name: 'Rename first chat' }).click();
  await list.getByLabel('Conversation title').fill('Renamed');
  await list.getByLabel('Conversation title').press('Enter');
  await expect(list.getByRole('button', { name: 'Renamed', exact: true })).toBeVisible();
  await list.getByRole('button', { name: 'Delete second chat' }).click();
  await list.getByRole('button', { name: 'Delete', exact: true }).click();
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(page.locator('.ls-msg-user')).toContainText('first chat');
});

test('restore after a reload: the transcript comes back, flagged as not in agent memory', async ({ page }) => {
  const r = await openPanel(page, 'demo');
  await send(page, 'please use a tool');
  await expect(lastReply(page)).toContainText('The demo tool returned');
  await idle(page);
  await r.reload();
  await expect(page.getByRole('button', { name: 'Conversations' })).toContainText('please use a tool');
  await expect(page.locator('.ls-msg-user')).toContainText('please use a tool');
  await expect(page.locator('.ls-tool-name')).toHaveText('demo_lookup');
  await expect(lastReply(page)).toContainText('The demo tool returned');
  await expect(page.getByRole('note')).toContainText('The agent may not remember the conversation above');
  // The restored conversation keeps working.
  await expect(page.getByRole('status').filter({ hasText: 'Ready' })).toBeVisible({ timeout: 60_000 });
  await send(page, 'think about it');
  await expect(lastReply(page)).toContainText('Done reasoning');
});

test('a pending interrupt does not survive a reload: its card expires', async ({ page }) => {
  const r = await openPanel(page, 'demo');
  await send(page, 'ask me');
  await expect(card(page).getByRole('button', { name: 'Approve' })).toBeVisible();
  await r.reload();
  await expect(card(page)).toContainText('No longer pending');
  await expect(card(page).getByRole('button')).toHaveCount(0);
  await expect(page.getByText('Answer the request above to continue.')).toHaveCount(0);
});

test('no agent configured: the status offers the demo', async ({ page }) => {
  relay = new Relay(page, { agent: 'none' });
  await relay.open();
  await expect(page.getByRole('alert')).toContainText('No agent configured');
  await page.getByRole('button', { name: 'Try the demo' }).click();
  await expect(page.getByRole('status')).toContainText('Ready · demo agent');
});

test('a bad agent spec shows the startup error verbatim', async ({ page }) => {
  relay = new Relay(page, { agent: 'does_not_exist.py:graph' });
  await relay.open();
  await expect(page.getByRole('alert')).toContainText('The agent failed to start');
  await expect(page.getByRole('alert')).toContainText('does_not_exist.py');
});
