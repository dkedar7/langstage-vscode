/**
 * The LangStage panel's host-side logic (ADR 0001): it owns the panel's own
 * `SidecarClient`, the conversation ↔ `session_id` map and each conversation's
 * transcript log, and relays between the webview protocol (shared/panelProtocol.ts)
 * and the sidecar's NDJSON commands and frames.
 *
 * No `vscode` import: `PanelController` supplies the editor-specific pieces through
 * `PanelDeps`, so this runs under `node --test` and in the scripted real-sidecar check.
 */
import { randomUUID } from 'crypto';
import {
  ConversationInfo,
  Frame,
  HostToWebview,
  LogEntry,
  PanelStatus,
  WebviewToHost,
  appendLog,
  isOpenableUrl,
  parseWebviewMessage,
} from '../shared/panelProtocol';
import { SidecarClient, SidecarStatus, isNoAgentError } from '../sidecar';

export interface PanelDeps {
  /** Deliver a message to the webview (a no-op while no view is resolved). */
  post(msg: HostToWebview): void;
  /** Build a (not yet started) client from the current settings. */
  createClient(opts: { demo: boolean }): SidecarClient;
  /** The configured agent spec, for the status line. */
  agentSpec(): string;
  openExternal(url: string): void;
  openSettings(): void;
  copy(text: string): void;
}

interface Conversation {
  id: string;
  title: string;
  /** The sidecar `session_id`, hence the LangGraph `thread_id`. */
  sessionId: string;
  log: LogEntry[];
}

const TITLE_MAX = 60;

export class PanelSession {
  private client: SidecarClient | undefined;
  private clientSubs: Array<{ dispose(): void }> = [];
  private demo = false;
  private status: PanelStatus = { phase: 'idle' };
  private conversations = new Map<string, Conversation>();
  private activeId: string;
  private turns = new Map<string, 'queued' | 'running'>();

  constructor(private readonly deps: PanelDeps) {
    this.activeId = this.createConversation().id;
  }

  /** Handle one raw message from the webview. Malformed messages are dropped. */
  handle(raw: unknown): void {
    const msg = parseWebviewMessage(raw);
    if (!msg) return;
    this.dispatch(msg);
  }

  /** Start the sidecar now (the view opened), so the status line is meaningful. */
  warmUp(): void {
    this.ensureClient();
  }

  /** Restart the panel's sidecar: after a settings change, or on request. */
  restart(): void {
    this.dropClient();
    this.ensureClient();
  }

  newConversation(): void {
    // Without a conversation list (M4) an idle, inactive conversation is unreachable,
    // so drop those; one with a turn still streaming is kept until it ends.
    for (const [id] of this.conversations) {
      if (!this.turns.has(id)) this.conversations.delete(id);
    }
    this.activeId = this.createConversation().id;
    this.postRestore();
  }

  dispose(): void {
    this.dropClient();
  }

  // ------------------------------------------------------------------ internals

  private dispatch(msg: WebviewToHost): void {
    switch (msg.type) {
      case 'ui/ready':
        this.ensureClient();
        this.postRestore();
        return;
      case 'send': {
        const conv = this.conversations.get(msg.conversationId);
        const text = msg.text.trim();
        if (!conv || !text || this.turns.has(conv.id)) return;
        const at = Date.now();
        appendLog(conv.log, { kind: 'user', text, at });
        if (conv.title === 'New conversation') conv.title = titleFrom(text);
        this.deps.post({ v: 1, type: 'user', conversationId: conv.id, text, at });
        this.startTurn(conv, { type: 'message', content: text });
        return;
      }
      case 'decide': {
        const conv = this.conversations.get(msg.conversationId);
        if (!conv || this.turns.has(conv.id) || !msg.decisions.length) return;
        // The sidecar validates the verbs with core's normalize_decision (gh #117).
        this.startTurn(conv, { type: 'decision', decisions: msg.decisions });
        return;
      }
      case 'cancel': {
        const conv = this.conversations.get(msg.conversationId);
        if (conv) this.client?.cancel(conv.sessionId);
        return;
      }
      case 'conversation/new':
        this.newConversation();
        return;
      case 'conversation/switch':
        if (this.conversations.has(msg.conversationId)) {
          this.activeId = msg.conversationId;
          this.postRestore();
        }
        return;
      case 'conversation/delete':
        if (this.conversations.has(msg.conversationId) && !this.turns.has(msg.conversationId)) {
          this.conversations.delete(msg.conversationId);
          if (this.activeId === msg.conversationId) {
            this.activeId =
              [...this.conversations.keys()].pop() ?? this.createConversation().id;
          }
          this.postRestore();
        }
        return;
      case 'conversation/rename': {
        const conv = this.conversations.get(msg.conversationId);
        const title = msg.title.trim();
        if (conv && title) {
          conv.title = title.slice(0, TITLE_MAX);
          this.postRestore();
        }
        return;
      }
      case 'openExternal':
        if (isOpenableUrl(msg.url)) this.deps.openExternal(msg.url);
        return;
      case 'openSettings':
        this.deps.openSettings();
        return;
      case 'copy':
        this.deps.copy(msg.text);
        return;
      case 'restartSidecar':
        this.restart();
        return;
      case 'tryDemo':
        this.demo = true;
        this.restart();
        return;
    }
  }

  private createConversation(): Conversation {
    const conv: Conversation = {
      id: `c-${randomUUID()}`,
      title: 'New conversation',
      sessionId: `vscode-${randomUUID()}`,
      log: [],
    };
    this.conversations.set(conv.id, conv);
    return conv;
  }

  private startTurn(conv: Conversation, command: Record<string, unknown>): void {
    const client = this.ensureClient();
    this.turns.set(conv.id, 'queued');
    this.deps.post({ v: 1, type: 'turn/queued', conversationId: conv.id });
    const turn = client.runTurn(conv.sessionId, command, {
      onStart: () => {
        this.turns.set(conv.id, 'running');
        this.deps.post({ v: 1, type: 'turn/started', conversationId: conv.id });
      },
      onFrame: (frame) => this.pushFrame(conv, frame),
    });
    turn.done.then(
      (result) => {
        if (result.cancelledBeforeStart) this.pushFrame(conv, { type: 'cancelled', source: 'panel' });
        this.endTurn(conv);
      },
      (err: unknown) => {
        const error = err instanceof Error ? err.message : String(err);
        // A host-side failure (the sidecar couldn't start, or died mid-turn) is shown
        // in the transcript like an agent error; `source` tells them apart.
        this.pushFrame(conv, { type: 'error', error, source: 'panel' });
        this.endTurn(conv, error);
      },
    );
  }

  private endTurn(conv: Conversation, error?: string): void {
    this.turns.delete(conv.id);
    this.deps.post({ v: 1, type: 'turn/ended', conversationId: conv.id, ...(error ? { error } : {}) });
  }

  private pushFrame(conv: Conversation, frame: Frame): void {
    appendLog(conv.log, { kind: 'frame', frame });
    this.deps.post({ v: 1, type: 'frame', conversationId: conv.id, frame });
  }

  private ensureClient(): SidecarClient {
    if (this.client && this.client.alive) return this.client;
    this.dropClient();
    const client = this.deps.createClient({ demo: this.demo });
    this.client = client;
    this.clientSubs = [
      client.onStatus((s) => {
        if (this.client === client) this.setStatus(s);
      }),
    ];
    client.start().catch(() => undefined); // the failure arrives through onStatus
    return client;
  }

  private dropClient(): void {
    const client = this.client;
    this.client = undefined;
    for (const s of this.clientSubs) s.dispose();
    this.clientSubs = [];
    client?.dispose();
  }

  private setStatus(s: SidecarStatus): void {
    this.status = {
      phase: s.state,
      error: s.error,
      stderrTail: s.stderrTail,
      noAgent: s.state === 'failed' && !this.demo && isNoAgentError(s.error),
      demo: this.demo,
      agentSpec: this.deps.agentSpec(),
    };
    this.deps.post({ v: 1, type: 'status', status: this.status });
  }

  private postRestore(): void {
    const conversations: ConversationInfo[] = [...this.conversations.values()].map((c) => ({
      id: c.id,
      title: c.title,
    }));
    const transcripts: Record<string, LogEntry[]> = {};
    for (const c of this.conversations.values()) transcripts[c.id] = c.log;
    this.deps.post({
      v: 1,
      type: 'restore',
      conversations,
      activeId: this.activeId,
      transcripts,
      turns: Object.fromEntries(this.turns),
      status: this.status,
    });
  }
}

function titleFrom(text: string): string {
  const line = text.split(/\r?\n/)[0].trim();
  return line.length > TITLE_MAX ? line.slice(0, TITLE_MAX - 1) + '…' : line || 'New conversation';
}
