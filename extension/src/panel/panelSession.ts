/**
 * The LangStage panel's host-side logic (ADR 0001): it owns the panel's own
 * `SidecarClient`, the conversations (each with its own `session_id`) and their
 * transcript logs, and relays between the webview protocol (shared/panelProtocol.ts)
 * and the sidecar's NDJSON commands and frames.
 *
 * - **Conversations (M4).** New / switch / rename / delete. Every conversation mints its
 *   own `vscode-<uuid>` session id, so agent memory is never shared between two of them.
 *   With a `ConversationStore` they persist per workspace and come back on reload.
 * - **One turn at a time.** The sidecar serves one turn per process, so a message in
 *   conversation B while A streams is queued here (`turn/queued`), in the client's own
 *   queue rather than the sidecar's stash, and Stop cancels only its own conversation.
 * - **Memory honesty.** When the sidecar process serving a conversation is replaced (a
 *   window reload, a restart, a crash), a `memory_reset` frame is logged: an in-memory
 *   checkpointer has forgotten the conversation, and any pending interrupt is gone.
 *
 * No `vscode` import: `PanelController` supplies the editor-specific pieces through
 * `PanelDeps`, so this runs under `node --test`, the Playwright harness and the scripted
 * real-sidecar check.
 */
import { randomUUID } from 'crypto';
import {
  ConversationInfo,
  Frame,
  HostToWebview,
  LogEntry,
  MEMORY_RESET_FRAME,
  PanelStatus,
  WebviewToHost,
  appendLog,
  isOpenableUrl,
  parseWebviewMessage,
} from '../shared/panelProtocol';
import { SidecarClient, SidecarStatus, isNoAgentError } from '../sidecar';
import { ConversationStore, StoredState } from './conversationStore';

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
  /** Where conversations persist; omitted = memory only. */
  store?: ConversationStore;
}

interface Conversation {
  id: string;
  title: string;
  /** The sidecar `session_id`, hence the LangGraph `thread_id`. */
  sessionId: string;
  createdAt: number;
  updatedAt: number;
  log: LogEntry[];
}

const TITLE_MAX = 60;
const UNTITLED = 'New conversation';

export class PanelSession {
  private client: SidecarClient | undefined;
  private clientWasReady = false;
  private clientSubs: Array<{ dispose(): void }> = [];
  private demo = false;
  private status: PanelStatus = { phase: 'idle' };
  private conversations = new Map<string, Conversation>();
  private activeId: string;
  private turns = new Map<string, 'queued' | 'running'>();
  private readonly store: ConversationStore | undefined;

  constructor(private readonly deps: PanelDeps) {
    this.store = deps.store;
    const saved = this.store?.load() ?? { conversations: [] };
    for (const c of saved.conversations) this.conversations.set(c.id, { ...c });
    // Whatever these transcripts say, this is a new sidecar process: an in-memory
    // checkpointer remembers none of it, and no interrupt is pending in it.
    this.markMemoryReset();
    this.activeId = saved.activeId ?? this.newest()?.id ?? this.createConversation().id;
    this.store?.bind(() => this.snapshot());
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

  /** Open a fresh conversation (reusing the active one if it is still empty). */
  newConversation(): void {
    const active = this.conversations.get(this.activeId);
    if (!active || active.log.length > 0 || this.turns.has(active.id)) {
      this.activeId = this.createConversation().id;
      this.store?.touchIndex();
    }
    this.postRestore();
  }

  dispose(): void {
    this.dropClient();
    this.store?.dispose();
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
        this.log(conv, { kind: 'user', text, at });
        const named = conv.title === UNTITLED;
        if (named) conv.title = titleFrom(text);
        this.deps.post({
          v: 1,
          type: 'user',
          conversationId: conv.id,
          text,
          at,
          ...(named ? { title: conv.title } : {}),
        });
        this.startTurn(conv, { type: 'message', content: text });
        return;
      }
      case 'decide': {
        const conv = this.conversations.get(msg.conversationId);
        if (!conv || this.turns.has(conv.id)) return;
        const at = Date.now();
        this.log(conv, { kind: 'decision', decisions: msg.decisions, at });
        this.deps.post({ v: 1, type: 'decision', conversationId: conv.id, decisions: msg.decisions, at });
        // The sidecar validates the verbs with core's normalize_decision (gh #117) and
        // refuses one the interrupt doesn't allow: `error → turn_end`, no `ack`, and the
        // interrupt stays pending.
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
        if (this.conversations.has(msg.conversationId) && this.activeId !== msg.conversationId) {
          this.activeId = msg.conversationId;
          this.store?.touchIndex();
          this.postRestore();
        }
        return;
      case 'conversation/delete': {
        const id = msg.conversationId;
        // A conversation with a turn in flight must be stopped first.
        if (!this.conversations.has(id) || this.turns.has(id)) return;
        this.conversations.delete(id);
        this.store?.remove(id);
        // Delete removes the transcript only; the checkpointer's thread is not touched.
        if (this.activeId === id) this.activeId = this.newest()?.id ?? this.createConversation().id;
        this.postRestore();
        return;
      }
      case 'conversation/rename': {
        const conv = this.conversations.get(msg.conversationId);
        const title = msg.title.trim().replace(/\s+/g, ' ');
        if (conv && title) {
          conv.title = title.slice(0, TITLE_MAX);
          this.store?.touchIndex();
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
    const now = Date.now();
    const conv: Conversation = {
      id: `c-${randomUUID()}`,
      title: UNTITLED,
      sessionId: `vscode-${randomUUID()}`,
      createdAt: now,
      updatedAt: now,
      log: [],
    };
    this.conversations.set(conv.id, conv);
    return conv;
  }

  private newest(): Conversation | undefined {
    let best: Conversation | undefined;
    for (const c of this.conversations.values()) if (!best || c.updatedAt >= best.updatedAt) best = c;
    return best;
  }

  private log(conv: Conversation, entry: LogEntry): void {
    appendLog(conv.log, entry);
    conv.updatedAt = Date.now();
    this.store?.touch(conv.id);
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
    // Only a deleted conversation is missing here (a turn outlives nothing else).
    if (this.conversations.get(conv.id) !== conv) return;
    this.log(conv, { kind: 'frame', frame });
    this.deps.post({ v: 1, type: 'frame', conversationId: conv.id, frame });
  }

  /**
   * The sidecar that served these conversations is gone. Log a `memory_reset` on every
   * conversation with history (once: not twice in a row), so the transcript says where
   * the agent's memory stops.
   */
  private markMemoryReset(): void {
    for (const conv of this.conversations.values()) {
      const last = conv.log[conv.log.length - 1];
      if (!last || (last.kind === 'frame' && last.frame.type === MEMORY_RESET_FRAME.type)) continue;
      this.pushFrame(conv, { ...MEMORY_RESET_FRAME });
    }
  }

  private ensureClient(): SidecarClient {
    if (this.client && this.client.alive) return this.client;
    this.dropClient();
    const client = this.deps.createClient({ demo: this.demo });
    this.client = client;
    this.clientWasReady = false;
    this.clientSubs = [
      client.onStatus((s) => {
        if (this.client !== client) return;
        if (s.state === 'ready') this.clientWasReady = true;
        this.setStatus(s);
      }),
    ];
    client.start().catch(() => undefined); // the failure arrives through onStatus
    return client;
  }

  private dropClient(): void {
    const client = this.client;
    const wasReady = this.clientWasReady;
    this.client = undefined;
    this.clientWasReady = false;
    for (const s of this.clientSubs) s.dispose();
    this.clientSubs = [];
    client?.dispose();
    // A process that served turns took their in-memory state with it.
    if (wasReady) this.markMemoryReset();
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
    // The process died on its own (not a restart we asked for): the next turn gets a
    // fresh one, which remembers nothing.
    if (s.state === 'stopped' && this.clientWasReady) {
      this.clientWasReady = false;
      this.markMemoryReset();
    }
  }

  private snapshot(): StoredState {
    return {
      activeId: this.activeId,
      conversations: [...this.conversations.values()].map((c) => ({ ...c })),
    };
  }

  private postRestore(): void {
    const conversations: ConversationInfo[] = [...this.conversations.values()].map((c) => ({
      id: c.id,
      title: c.title,
      updatedAt: c.updatedAt,
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
      persistent: this.store?.persistent ?? false,
    });
  }
}

function titleFrom(text: string): string {
  const line = text.split(/\r?\n/)[0].trim();
  return line.length > TITLE_MAX ? line.slice(0, TITLE_MAX - 1) + '…' : line || UNTITLED;
}
