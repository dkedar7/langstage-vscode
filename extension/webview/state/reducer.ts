/**
 * The panel's view model: a pure `(state, message) → state` reducer over the host's
 * messages (shared/panelProtocol.ts), with sidecar frames folded in by `reduceFrame`.
 *
 * Ported from the frame dispatch in langstage/frontend `src/hooks/useAgentStream.ts`
 * (langstage @ 0387426), minus its transport and localStorage: the same frame
 * vocabulary (content / reasoning / tool_start / tool_end / extraction / interrupt /
 * complete / cancelled / error) drives the same message and tool-call model. Unknown
 * frame types and keys are ignored. No DOM or React imports, so it runs under
 * `node --test`.
 */
import type {
  ConversationInfo,
  Frame,
  HostToWebview,
  LogEntry,
  PanelStatus,
} from '../../src/shared/panelProtocol';

export interface TodoItem {
  content: string;
  status: 'pending' | 'in_progress' | 'completed';
}

export interface ToolItem {
  kind: 'tool';
  key: number;
  id: string;
  name: string;
  args: unknown;
  status: 'running' | 'success' | 'error';
  result?: string;
  errorMessage?: string | null;
  durationMs?: number | null;
  extraction?: { type: string; data: unknown };
}

export type Item =
  | { kind: 'user'; key: number; text: string; at?: number }
  | { kind: 'assistant'; key: number; messageId?: string; text: string; streaming: boolean }
  | { kind: 'reasoning'; key: number; text: string; streaming: boolean }
  | ToolItem
  | InterruptItem
  | { kind: 'error'; key: number; error: string; traceback?: string; host: boolean }
  | { kind: 'notice'; key: number; text: string }
  /** The sidecar serving this conversation was replaced: its memory stops here. */
  | { kind: 'memoryReset'; key: number };

export interface InterruptItem {
  kind: 'interrupt';
  key: number;
  frame: Frame;
  /** How it was answered, once the sidecar accepted the decision (`ack`). */
  answer?: Array<Record<string, unknown>>;
  /** The agent restarted before it was answered: it can no longer be. */
  expired?: boolean;
}

/** The interrupt a conversation is paused on (M3). */
export interface Pending {
  /** The `key` of its interrupt item. */
  key: number;
  frame: Frame;
  /** The decision on its way to the sidecar, until `ack` (accepted) or `error` (refused). */
  sent?: Array<Record<string, unknown>>;
  /** The sidecar refused the last answer (`error → turn_end`, no `ack`); still pending. */
  error?: string;
}

export type TurnPhase = 'idle' | 'queued' | 'running';

export interface ConversationView {
  id: string;
  title: string;
  items: Item[];
  /** The live checklist from the latest `todos` extraction, updated in place. */
  todos: TodoItem[] | null;
  turn: TurnPhase;
  /** Set by an `interrupt` frame; cleared once the sidecar accepts a command (`ack`). */
  pending?: Pending;
  nextKey: number;
}

export interface PanelState {
  conversations: Record<string, ConversationView>;
  order: string[];
  activeId: string | undefined;
  status: PanelStatus;
  /** Transcripts survive a window reload (workspace storage). */
  persistent: boolean;
}

export const initialState: PanelState = {
  conversations: {},
  order: [],
  activeId: undefined,
  status: { phase: 'idle' },
  persistent: false,
};

export function emptyConversation(info: ConversationInfo): ConversationView {
  return { id: info.id, title: info.title, items: [], todos: null, turn: 'idle', nextKey: 1 };
}

/** The reducer the webview runs over every host message. */
export function reduce(state: PanelState, msg: HostToWebview): PanelState {
  switch (msg.type) {
    case 'restore': {
      const conversations: Record<string, ConversationView> = {};
      for (const info of msg.conversations) {
        let conv = emptyConversation(info);
        for (const entry of msg.transcripts[info.id] ?? []) conv = reduceLogEntry(conv, entry);
        const turn = msg.turns[info.id];
        conv = turn ? { ...conv, turn } : settle(conv);
        conversations[info.id] = conv;
      }
      // Newest first, for the conversation list.
      const order = msg.conversations
        .map((c, i) => ({ id: c.id, at: c.updatedAt ?? 0, i }))
        .sort((a, b) => b.at - a.at || b.i - a.i)
        .map((c) => c.id);
      return {
        conversations,
        order,
        activeId: msg.activeId,
        status: msg.status,
        persistent: msg.persistent ?? false,
      };
    }
    case 'status':
      return { ...state, status: msg.status };
    case 'user':
      return update(state, msg.conversationId, (c) =>
        reduceLogEntry(msg.title ? { ...c, title: msg.title } : c, { kind: 'user', text: msg.text, at: msg.at }),
      );
    case 'decision':
      return update(state, msg.conversationId, (c) =>
        reduceLogEntry(c, { kind: 'decision', decisions: msg.decisions, at: msg.at }),
      );
    case 'frame':
      return update(state, msg.conversationId, (c) => reduceFrame(c, msg.frame));
    case 'turn/queued':
      return update(state, msg.conversationId, (c) => ({ ...c, turn: 'queued' }));
    case 'turn/started':
      return update(state, msg.conversationId, (c) => ({ ...c, turn: 'running' }));
    case 'turn/ended':
      return update(state, msg.conversationId, (c) => unsend(settle({ ...c, turn: 'idle' })));
    default:
      return state;
  }
}

function update(
  state: PanelState,
  id: string,
  fn: (c: ConversationView) => ConversationView,
): PanelState {
  const conv = state.conversations[id];
  if (!conv) return state;
  const next = fn(conv);
  return next === conv ? state : { ...state, conversations: { ...state.conversations, [id]: next } };
}

export function reduceLogEntry(conv: ConversationView, entry: LogEntry): ConversationView {
  if (entry.kind === 'user') {
    return push(conv, { kind: 'user', key: 0, text: entry.text, at: entry.at });
  }
  if (entry.kind === 'decision') {
    // Shown on the card once accepted; until then the card says it is being sent.
    return conv.pending
      ? { ...conv, pending: { ...conv.pending, sent: entry.decisions, error: undefined } }
      : conv;
  }
  return reduceFrame(conv, entry.frame);
}

function push(conv: ConversationView, item: Item): ConversationView {
  // Anything new closes the streaming block before it.
  const { items } = settle(conv);
  return { ...conv, items: [...items, { ...item, key: conv.nextKey } as Item], nextKey: conv.nextKey + 1 };
}

function replaceLast(conv: ConversationView, item: Item): ConversationView {
  const items = conv.items.slice(0, -1);
  items.push(item);
  return { ...conv, items };
}

/** Close every streaming block (a turn ended, completed or was cancelled). */
function settle(conv: ConversationView): ConversationView {
  if (!conv.items.some((it) => (it.kind === 'assistant' || it.kind === 'reasoning') && it.streaming)) {
    return conv;
  }
  return {
    ...conv,
    items: conv.items.map((it) =>
      (it.kind === 'assistant' || it.kind === 'reasoning') && it.streaming ? { ...it, streaming: false } : it,
    ),
  };
}

const asString = (x: unknown): string | undefined => (typeof x === 'string' ? x : undefined);

/** Fold one sidecar frame into a conversation. Unknown frames leave it unchanged. */
export function reduceFrame(conv: ConversationView, frame: Frame): ConversationView {
  switch (frame.type) {
    case 'content': {
      const text = asString(frame.content) ?? '';
      if (!text) return conv;
      const messageId = asString(frame.message_id);
      const last = conv.items[conv.items.length - 1];
      // gh #108: a new `message_id` is a new assistant message (e.g. a planner node,
      // then an answer node), so it starts its own block instead of gluing on. Chunks
      // of one message share an id; a frame without one never adds a break.
      if (
        last?.kind === 'assistant' &&
        last.streaming &&
        (messageId === undefined || last.messageId === undefined || last.messageId === messageId)
      ) {
        return replaceLast(conv, { ...last, text: last.text + text, messageId: last.messageId ?? messageId });
      }
      return push(conv, { kind: 'assistant', key: 0, messageId, text, streaming: true });
    }
    case 'reasoning': {
      const text = asString(frame.content) ?? '';
      if (!text) return conv;
      const last = conv.items[conv.items.length - 1];
      if (last?.kind === 'reasoning' && last.streaming) {
        return replaceLast(conv, { ...last, text: last.text + text });
      }
      return push(conv, { kind: 'reasoning', key: 0, text, streaming: true });
    }
    case 'tool_start': {
      const id = asString(frame.id) ?? `tool-${conv.nextKey}`;
      return push(conv, {
        kind: 'tool',
        key: 0,
        id,
        name: asString(frame.name) ?? 'tool',
        args: frame.args ?? {},
        status: 'running',
      });
    }
    case 'tool_end': {
      const id = asString(frame.id);
      const done = {
        status: frame.status === 'error' ? ('error' as const) : ('success' as const),
        result: frame.result === undefined || frame.result === null ? undefined : stringify(frame.result),
        errorMessage: asString(frame.error_message) ?? null,
        durationMs: typeof frame.duration_ms === 'number' ? frame.duration_ms : null,
      };
      const idx = findLastIndex(conv.items, (it) => it.kind === 'tool' && it.id === id);
      if (idx < 0) {
        // A tool_end with no tool_start (e.g. a snapshot-path tool): still show it.
        return push(conv, {
          kind: 'tool',
          key: 0,
          id: id ?? `tool-${conv.nextKey}`,
          name: asString(frame.name) ?? 'tool',
          args: {},
          ...done,
        });
      }
      const items = conv.items.slice();
      items[idx] = { ...(items[idx] as ToolItem), ...done };
      return { ...conv, items };
    }
    case 'extraction': {
      const type = asString(frame.extracted_type) ?? 'data';
      let next = conv;
      if (type === 'todos') next = { ...next, todos: normalizeTodos(frame.data) };
      // Attach to the call it came from: by id when the frame has one, else the latest
      // call of that tool (core emits extraction right after that call's tool_end).
      const id = asString(frame.id);
      const toolName = asString(frame.tool_name);
      const idx = findLastIndex(
        next.items,
        (it) => it.kind === 'tool' && (id !== undefined ? it.id === id : it.name === toolName),
      );
      if (idx >= 0) {
        const items = next.items.slice();
        items[idx] = { ...(items[idx] as ToolItem), extraction: { type, data: frame.data } };
        next = { ...next, items };
      }
      return next;
    }
    case 'ack': {
      // The sidecar accepted a command, so nothing is pending any more: a decision
      // resumed the agent (record it on its card).
      const p = conv.pending;
      if (!p) return conv;
      const items = p.sent
        ? conv.items.map((it) => (it.kind === 'interrupt' && it.key === p.key ? { ...it, answer: p.sent } : it))
        : conv.items;
      return { ...conv, items, pending: undefined };
    }
    case 'interrupt': {
      const next = push(conv, { kind: 'interrupt', key: 0, frame });
      return { ...next, pending: { key: conv.nextKey, frame } };
    }
    case 'memory_reset': {
      // The sidecar was replaced: an unanswered interrupt died with it.
      const p = conv.pending;
      const items = p
        ? conv.items.map((it) => (it.kind === 'interrupt' && it.key === p.key ? { ...it, expired: true } : it))
        : conv.items;
      return push({ ...conv, items, pending: undefined }, { kind: 'memoryReset', key: 0 });
    }
    case 'error':
      // The sidecar refused what answered the pending interrupt (gh #117 / #134):
      // `error → turn_end` with no `ack`. The interrupt stays pending, so the card
      // stays live and shows why.
      if (conv.pending && frame.source !== 'panel') {
        return {
          ...conv,
          pending: { ...conv.pending, sent: undefined, error: asString(frame.error) ?? 'refused' },
        };
      }
      return push(unsend(conv), {
        kind: 'error',
        key: 0,
        error: asString(frame.error) ?? 'unknown error',
        traceback: asString(frame.traceback),
        host: frame.source === 'panel',
      });
    case 'cancelled':
      return push(unsend(settle(conv)), { kind: 'notice', key: 0, text: 'Stopped.' });
    case 'complete':
      return settle(conv);
    case 'turn_end':
      return unsend(settle(conv));
    default:
      // ack, ready, usage and anything newer: nothing to render.
      return conv;
  }
}

/** A turn ended without the sidecar accepting the decision in flight: it wasn't sent. */
function unsend(conv: ConversationView): ConversationView {
  return conv.pending?.sent ? { ...conv, pending: { ...conv.pending, sent: undefined } } : conv;
}

/** Todos from a `todos` extraction: a list, or `{todos: [...]}`; items may use `task`/`done`. */
export function normalizeTodos(data: unknown): TodoItem[] {
  const raw: unknown[] = Array.isArray(data)
    ? data
    : data && typeof data === 'object' && Array.isArray((data as { todos?: unknown }).todos)
      ? ((data as { todos: unknown[] }).todos)
      : [];
  return raw.map((item) => {
    if (item && typeof item === 'object') {
      const o = item as Record<string, unknown>;
      const status =
        o.status === 'completed' || o.status === 'in_progress' || o.status === 'pending'
          ? o.status
          : o.done === true
            ? 'completed'
            : 'pending';
      return { content: asString(o.content) ?? asString(o.task) ?? '', status };
    }
    return { content: String(item), status: 'pending' as const };
  });
}

/** The interrupt the conversation is paused on, if any. */
export function pendingInterrupt(conv: ConversationView | undefined): Pending | undefined {
  return conv?.pending;
}

function stringify(x: unknown): string {
  return typeof x === 'string' ? x : JSON.stringify(x, null, 2);
}

function findLastIndex<T>(arr: T[], pred: (x: T) => boolean): number {
  for (let i = arr.length - 1; i >= 0; i--) if (pred(arr[i])) return i;
  return -1;
}
