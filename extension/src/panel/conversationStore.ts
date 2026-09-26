/**
 * ConversationStore (build plan M4): the panel's conversations, persisted per workspace.
 *
 * Layout under `dir` (the extension's `context.storageUri`, which is per-workspace and
 * per-machine and is not synced):
 *
 *   conversations.json         { version: 1, activeId, conversations: [{id, title, sessionId, ...}] }
 *   transcripts/<id>.json      { version: 1, log: LogEntry[] }   (the coalesced transcript log)
 *
 * Writes are debounced and atomic (write a temp file, then rename), and `flush()` writes
 * everything pending at once (on dispose). A missing or corrupt file is treated as
 * empty: the panel must open even if its storage is damaged. With no `dir` (a window
 * with no folder open, where VS Code gives no `storageUri`) nothing is persisted.
 *
 * No `vscode` import, so it runs under `node --test`.
 */
import * as fs from 'fs';
import * as path from 'path';
import type { LogEntry } from '../shared/panelProtocol';

export interface StoredConversation {
  id: string;
  title: string;
  /** The sidecar `session_id` (the LangGraph `thread_id`); one per conversation, never shared. */
  sessionId: string;
  createdAt: number;
  updatedAt: number;
  log: LogEntry[];
}

export interface StoredState {
  activeId?: string;
  conversations: StoredConversation[];
}

const VERSION = 1;
const INDEX = 'conversations.json';
const TRANSCRIPTS = 'transcripts';
const ID_RE = /^c-[0-9a-f-]{8,64}$/i;

/** A conversation id is also a file name, so only ids the host mints are accepted. */
export function isConversationId(id: unknown): id is string {
  return typeof id === 'string' && ID_RE.test(id);
}

export class ConversationStore {
  private timer: NodeJS.Timeout | undefined;
  private indexDirty = false;
  private readonly dirty = new Set<string>();
  private readonly removed = new Set<string>();
  private snapshot: (() => StoredState) | undefined;

  constructor(
    private readonly dir: string | undefined,
    private readonly debounceMs = 400,
  ) {}

  /** Whether transcripts survive a reload (false with no workspace storage). */
  get persistent(): boolean {
    return this.dir !== undefined;
  }

  /** Read everything back. Corrupt or foreign entries are skipped, never thrown. */
  load(): StoredState {
    if (!this.dir) return { conversations: [] };
    const index = readJson(path.join(this.dir, INDEX)) as
      | { version?: unknown; activeId?: unknown; conversations?: unknown }
      | undefined;
    if (!index || index.version !== VERSION || !Array.isArray(index.conversations)) {
      return { conversations: [] };
    }
    const conversations: StoredConversation[] = [];
    for (const raw of index.conversations) {
      if (!raw || typeof raw !== 'object') continue;
      const c = raw as Record<string, unknown>;
      if (!isConversationId(c.id) || typeof c.sessionId !== 'string' || !c.sessionId) continue;
      const t = readJson(this.transcriptPath(c.id)) as { log?: unknown } | undefined;
      const log = Array.isArray(t?.log) ? (t!.log as unknown[]).filter(isLogEntry) : [];
      conversations.push({
        id: c.id,
        title: typeof c.title === 'string' && c.title ? c.title : 'New conversation',
        sessionId: c.sessionId,
        createdAt: typeof c.createdAt === 'number' ? c.createdAt : 0,
        updatedAt: typeof c.updatedAt === 'number' ? c.updatedAt : 0,
        log,
      });
    }
    const activeId = conversations.some((c) => c.id === index.activeId)
      ? (index.activeId as string)
      : undefined;
    return { activeId, conversations };
  }

  /** Where the store reads the current state from when it writes. Set once by the owner. */
  bind(snapshot: () => StoredState): void {
    this.snapshot = snapshot;
  }

  /** The index (titles, order, active id) changed. */
  touchIndex(): void {
    this.indexDirty = true;
    this.schedule();
  }

  /** A conversation's transcript changed. */
  touch(id: string): void {
    this.dirty.add(id);
    this.removed.delete(id);
    this.indexDirty = true;
    this.schedule();
  }

  /** A conversation was deleted: drop its transcript file. */
  remove(id: string): void {
    this.dirty.delete(id);
    this.removed.add(id);
    this.indexDirty = true;
    this.schedule();
  }

  /** Write everything pending now. */
  flush(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = undefined;
    }
    if (!this.dir || !this.snapshot) return;
    if (!this.indexDirty && !this.dirty.size && !this.removed.size) return;
    const state = this.snapshot();
    try {
      fs.mkdirSync(path.join(this.dir, TRANSCRIPTS), { recursive: true });
      for (const id of this.removed) {
        try {
          fs.unlinkSync(this.transcriptPath(id));
        } catch {
          /* already gone */
        }
      }
      for (const c of state.conversations) {
        if (this.dirty.has(c.id)) writeAtomic(this.transcriptPath(c.id), { version: VERSION, log: c.log });
      }
      if (this.indexDirty) {
        writeAtomic(path.join(this.dir, INDEX), {
          version: VERSION,
          activeId: state.activeId,
          conversations: state.conversations.map(({ log: _log, ...meta }) => meta),
        });
      }
    } catch {
      // Storage is best effort: a full disk must not break the chat.
    }
    this.dirty.clear();
    this.removed.clear();
    this.indexDirty = false;
  }

  dispose(): void {
    this.flush();
  }

  private schedule(): void {
    if (!this.dir || this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.flush();
    }, this.debounceMs);
    this.timer.unref?.();
  }

  private transcriptPath(id: string): string {
    return path.join(this.dir!, TRANSCRIPTS, `${id}.json`);
  }
}

function readJson(file: string): unknown {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    return undefined;
  }
}

function writeAtomic(file: string, value: unknown): void {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, JSON.stringify(value));
  fs.renameSync(tmp, file);
}

function isLogEntry(x: unknown): x is LogEntry {
  if (!x || typeof x !== 'object') return false;
  const e = x as Record<string, unknown>;
  if (e.kind === 'user') return typeof e.text === 'string';
  if (e.kind === 'decision') return Array.isArray(e.decisions);
  if (e.kind === 'frame') {
    const f = e.frame as Record<string, unknown> | undefined;
    return !!f && typeof f === 'object' && typeof f.type === 'string';
  }
  return false;
}
