/**
 * The LangStage panel's webview ⇄ extension-host message protocol (ADR 0001, build
 * plan "Webview ⇄ host message protocol"). Extension-internal: both bundles import this
 * file, and every message carries `v: 1`.
 *
 * The host owns truth (the conversation ↔ `session_id` map, the transcript log, the
 * sidecar status). The webview can be torn down and rebuilt from `restore` at any time.
 *
 * No `vscode` or DOM imports: this runs in the host, the webview and `node --test`.
 */

export const PROTOCOL_VERSION = 1 as const;

/** A sidecar frame, forwarded verbatim. Unknown types and keys must be ignored. */
export interface Frame {
  type: string;
  [key: string]: unknown;
}

/** One entry of a conversation's transcript log: what the user sent, or a frame. */
export type LogEntry = { kind: 'user'; text: string; at?: number } | { kind: 'frame'; frame: Frame };

export type SidecarPhase = 'idle' | 'starting' | 'ready' | 'failed' | 'stopped';

export interface PanelStatus {
  phase: SidecarPhase;
  /** The startup error verbatim (gh #131), for `failed`. */
  error?: string;
  stderrTail?: string;
  /** The failure is "no agent configured": offer Open settings and Try the demo. */
  noAgent?: boolean;
  /** The panel runs the keyless `--demo=tools` agent this session. */
  demo?: boolean;
  /** The configured agent spec, for display ('' = langstage.toml / env). */
  agentSpec?: string;
}

export interface ConversationInfo {
  id: string;
  title: string;
}

// ---------------------------------------------------------------- webview → host

export type WebviewToHost =
  | { v: 1; type: 'ui/ready' }
  | { v: 1; type: 'send'; conversationId: string; text: string }
  | { v: 1; type: 'decide'; conversationId: string; decisions: Array<Record<string, unknown>> }
  | { v: 1; type: 'cancel'; conversationId: string }
  | { v: 1; type: 'conversation/new' }
  | { v: 1; type: 'conversation/switch'; conversationId: string }
  | { v: 1; type: 'conversation/delete'; conversationId: string }
  | { v: 1; type: 'conversation/rename'; conversationId: string; title: string }
  | { v: 1; type: 'openExternal'; url: string }
  | { v: 1; type: 'openSettings' }
  | { v: 1; type: 'restartSidecar' }
  | { v: 1; type: 'tryDemo' }
  | { v: 1; type: 'copy'; text: string };

// ---------------------------------------------------------------- host → webview

export type HostToWebview =
  | {
      v: 1;
      type: 'restore';
      conversations: ConversationInfo[];
      activeId: string;
      transcripts: Record<string, LogEntry[]>;
      /** Conversations with a turn in flight or queued. */
      turns: Record<string, 'queued' | 'running'>;
      status: PanelStatus;
    }
  | { v: 1; type: 'user'; conversationId: string; text: string; at: number }
  | { v: 1; type: 'frame'; conversationId: string; frame: Frame }
  | { v: 1; type: 'turn/queued'; conversationId: string }
  | { v: 1; type: 'turn/started'; conversationId: string }
  | { v: 1; type: 'turn/ended'; conversationId: string; error?: string }
  | { v: 1; type: 'status'; status: PanelStatus };

const str = (x: unknown): x is string => typeof x === 'string';

/**
 * Validate a message from the webview. The webview renders untrusted agent output, so
 * the host checks every field it acts on rather than trusting the shape.
 */
export function parseWebviewMessage(raw: unknown): WebviewToHost | undefined {
  if (!raw || typeof raw !== 'object') return undefined;
  const m = raw as Record<string, unknown>;
  if (m.v !== PROTOCOL_VERSION || !str(m.type)) return undefined;
  switch (m.type) {
    case 'ui/ready':
    case 'conversation/new':
    case 'openSettings':
    case 'restartSidecar':
    case 'tryDemo':
      return { v: 1, type: m.type };
    case 'send':
      return str(m.conversationId) && str(m.text)
        ? { v: 1, type: 'send', conversationId: m.conversationId, text: m.text }
        : undefined;
    case 'decide':
      return str(m.conversationId) &&
        Array.isArray(m.decisions) &&
        m.decisions.every((d) => d && typeof d === 'object' && !Array.isArray(d))
        ? {
            v: 1,
            type: 'decide',
            conversationId: m.conversationId,
            decisions: m.decisions as Array<Record<string, unknown>>,
          }
        : undefined;
    case 'cancel':
    case 'conversation/switch':
    case 'conversation/delete':
      return str(m.conversationId) ? { v: 1, type: m.type, conversationId: m.conversationId } : undefined;
    case 'conversation/rename':
      return str(m.conversationId) && str(m.title)
        ? { v: 1, type: 'conversation/rename', conversationId: m.conversationId, title: m.title }
        : undefined;
    case 'openExternal':
      return str(m.url) ? { v: 1, type: 'openExternal', url: m.url } : undefined;
    case 'copy':
      return str(m.text) ? { v: 1, type: 'copy', text: m.text } : undefined;
    default:
      return undefined;
  }
}

/** Only these link schemes may be opened from rendered agent output. */
export function isOpenableUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === 'https:' || u.protocol === 'http:' || u.protocol === 'mailto:';
  } catch {
    return false;
  }
}

/**
 * Append to a transcript log, coalescing streamed chunks so the log stays small: a
 * `content` frame joins the previous one when both carry the same `message_id` (or
 * neither does), and consecutive `reasoning` frames join. Replaying the coalesced log
 * through the webview reducer renders the same transcript as the live frames did.
 */
export function appendLog(log: LogEntry[], entry: LogEntry): void {
  const last = log[log.length - 1];
  if (entry.kind === 'frame' && last?.kind === 'frame') {
    const a = last.frame;
    const b = entry.frame;
    if (
      a.type === 'content' &&
      b.type === 'content' &&
      a.message_id === b.message_id &&
      typeof a.content === 'string' &&
      typeof b.content === 'string'
    ) {
      log[log.length - 1] = { kind: 'frame', frame: { ...a, content: a.content + b.content } };
      return;
    }
    if (
      a.type === 'reasoning' &&
      b.type === 'reasoning' &&
      typeof a.content === 'string' &&
      typeof b.content === 'string'
    ) {
      log[log.length - 1] = { kind: 'frame', frame: { ...a, content: a.content + b.content } };
      return;
    }
  }
  log.push(entry);
}
