/**
 * The harness relay (build plan M5): runs the panel's REAL host logic (PanelSession,
 * ConversationStore, SidecarClient from dist/) in the Playwright process, against a
 * spawned `python -m langstage_vscode`, and wires it to the harness page:
 *
 *   webview (harness page) --acquireVsCodeApi().postMessage--> window.__toHost
 *        --Playwright exposeFunction--> PanelSession.handle --> SidecarClient --> sidecar
 *   sidecar frames --> PanelSession --post--> page.evaluate(window.postMessage)
 *
 * The plan sketched a WebSocket relay server; Playwright's own page bridge does the same
 * job with no server and no port. Test-only; never in the VSIX.
 *
 * Python: $LANGSTAGE_PYTHON (a path or a command), default `python`; it must have
 * langstage-vscode installed (`pip install -e .` at the repo root).
 */
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import { pathToFileURL } from 'url';
import type { Page } from '@playwright/test';

// The compiled host modules (npm run compile). Loaded with require so this file does
// not pull the extension's tsconfig into Playwright's transpile.
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { PanelSession } = require('../../dist/panel/panelSession');
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { ConversationStore } = require('../../dist/panel/conversationStore');
// eslint-disable-next-line @typescript-eslint/no-var-requires
const { SidecarClient, sidecarArgs, sidecarEnv } = require('../../dist/sidecar');

export const HARNESS = pathToFileURL(path.join(__dirname, 'index.html')).href;
export const FIXTURE_AGENT = `${path.resolve(__dirname, '..', 'fixtures', 'hitl_agent.py')}:graph`;

export function pythonCommand(): string {
  const p = process.env.LANGSTAGE_PYTHON || 'python';
  // A path (not a bare command) is resolved here: the sidecar runs with cwd = workspace.
  return /[\\/]/.test(p) ? path.resolve(p) : p;
}

export interface PanelOptions {
  /**
   * 'demo': the keyless `--demo=tools` agent, chosen the way a user does (the status
   * line's "Try the demo"); 'none': no agent configured; else an agent spec.
   */
  agent: 'demo' | 'none' | string;
  /** Workspace storage for the ConversationStore (a temp dir by default). */
  storeDir?: string;
}

type HostMsg = { type: string; [k: string]: unknown };

export class Relay {
  /** Every message the host sent the webview, in order. */
  readonly posted: HostMsg[] = [];
  readonly workspace: string;
  readonly storeDir: string;
  private session: any;
  private delivery: Promise<unknown> = Promise.resolve();
  private closed = false;
  /**
   * The page has asked for its state (`ui/ready`). Until then nothing is delivered: the
   * `restore` that answers `ui/ready` carries everything, and a page that is still
   * loading would drop a message anyway.
   */
  private uiReady = false;

  constructor(
    private readonly page: Page,
    private readonly opts: PanelOptions,
  ) {
    this.workspace = fs.mkdtempSync(path.join(os.tmpdir(), 'ls-ws-'));
    this.storeDir = opts.storeDir ?? fs.mkdtempSync(path.join(os.tmpdir(), 'ls-store-'));
  }

  /** Bind the page to a fresh host session and open the harness. */
  async open(): Promise<void> {
    await this.page.exposeFunction('__toHost', (msg: { type?: string }) => {
      if (msg?.type === 'ui/ready') this.uiReady = true;
      this.session?.handle(msg);
    });
    this.session = this.createSession();
    await this.page.goto(HARNESS);
  }

  /**
   * A window reload: the host session and its sidecar go away, a new host session
   * starts over the same workspace storage (a new sidecar), and the webview is rebuilt.
   */
  async reload(): Promise<void> {
    this.uiReady = false;
    this.session.dispose();
    this.session = this.createSession();
    await this.page.reload();
  }

  /** Send a message to the host as the webview would (e.g. one the UI can't produce). */
  toHost(msg: Record<string, unknown>): void {
    this.session.handle({ v: 1, ...msg });
  }

  /** The active conversation id, from the latest `restore`. */
  activeId(): string {
    const r = [...this.posted].reverse().find((m) => m.type === 'restore');
    return String(r?.activeId);
  }

  /** The sidecar frames the host forwarded, optionally of one type. */
  frames(type?: string): Array<Record<string, unknown>> {
    return this.posted
      .filter((m) => m.type === 'frame')
      .map((m) => m.frame as Record<string, unknown>)
      .filter((f) => !type || f.type === type);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.session?.dispose();
  }

  private createSession(): any {
    const { agent } = this.opts;
    const python = pythonCommand();
    const workspace = this.workspace;
    // No agent from the environment: only what this test configures.
    const env = { ...process.env };
    delete env.LANGSTAGE_AGENT_SPEC;
    delete env.DEEPAGENT_AGENT_SPEC;
    const spec = agent === 'demo' || agent === 'none' ? '' : agent;
    const session = new PanelSession({
      post: (msg: HostMsg) => {
        this.posted.push(msg);
        if (process.env.RELAY_DEBUG) console.log(new Date().toISOString(), msg.type, JSON.stringify(msg).slice(0, 160));
        if (!this.uiReady) return;
        // Deliver in order, as VS Code's postMessage does.
        this.delivery = this.delivery.then(() =>
          this.page.evaluate((m) => window.postMessage(m, '*'), msg).catch(() => undefined),
        );
      },
      createClient: ({ demo }: { demo: boolean }) =>
        new SidecarClient({
          python,
          args: sidecarArgs(workspace, spec, demo ? 'tools' : undefined),
          cwd: workspace,
          env: sidecarEnv(workspace, env),
        }),
      agentSpec: () => spec,
      openExternal: () => undefined,
      openSettings: () => undefined,
      copy: () => undefined,
      store: new ConversationStore(this.storeDir, 50),
    });
    // The user's path to the demo: "Try the demo" on the status line.
    if (agent === 'demo') session.handle({ v: 1, type: 'tryDemo' });
    return session;
  }
}
