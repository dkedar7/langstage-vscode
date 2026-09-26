/**
 * SidecarClient: one long-lived `python -m langstage_vscode` process, shared by the
 * turns of one front end (ADR 0001). Both surfaces use it: the `@langstage` chat
 * participant and the LangStage panel each own their own client.
 *
 * It covers what `extension.ts` used to do inline:
 * - spawn, and gate every turn on the sidecar's one `ready` frame;
 * - startup-error capture: the `error` frame written before `ready`, else the tail of
 *   stderr (gh #131);
 * - routing each frame to the turn in flight (turns are serialized per process);
 * - a turn queue, so a second command waits for the first turn's `turn_end`;
 * - cooperative `cancel` (gh #67/#106), and `dispose`.
 *
 * This module has no `vscode` import, so it runs under `node --test` with a fake
 * child process (see test/sidecar.test.ts).
 */
import { spawn as nodeSpawn, ChildProcess, SpawnOptions } from 'child_process';
import { EventEmitter } from 'events';
import * as readline from 'readline';

/** One NDJSON frame from the sidecar. Unknown types and keys are passed through. */
export interface SidecarFrame {
  type: string;
  [key: string]: unknown;
}

export type SidecarState = 'idle' | 'starting' | 'ready' | 'failed' | 'stopped';

export interface SidecarStatus {
  state: SidecarState;
  /** The startup error: the pre-`ready` `error` frame's text, or the last stderr line. */
  error?: string;
  /** The last few KB of stderr, for a failed or stopped sidecar. */
  stderrTail?: string;
}

export type SpawnFn = (command: string, args: string[], options: SpawnOptions) => ChildProcess;

export interface SidecarOptions {
  /** The Python interpreter. */
  python: string;
  /** Arguments after the interpreter, e.g. from `sidecarArgs()`. */
  args: string[];
  cwd: string;
  env?: NodeJS.ProcessEnv;
  /** Injected in tests. */
  spawn?: SpawnFn;
  /** How long to wait after `exit` for `close` (and a late pre-`ready` error frame). */
  exitGraceMs?: number;
}

/** How a turn ended. */
export interface TurnResult {
  /** The sidecar accepted the command (`ack`). A refused command ends with no `ack`. */
  acked: boolean;
  /** The `interrupt` frame the turn paused on, if any. */
  interrupt?: SidecarFrame;
  /** The turn was cancelled while still queued, so it never reached the sidecar. */
  cancelledBeforeStart?: boolean;
}

export interface TurnHandlers {
  /** The command was written to the sidecar (it left the queue). */
  onStart?: () => void;
  /** Every frame of this turn, including the final `turn_end`. */
  onFrame?: (frame: SidecarFrame) => void;
}

export interface Turn {
  sessionId: string;
  /** Resolves on `turn_end`; rejects if the sidecar can't start or exits mid-turn. */
  done: Promise<TurnResult>;
}

/** The sidecar command line (after the interpreter) for a workspace and agent. */
export function sidecarArgs(
  workspace: string,
  agentSpec: string,
  demo?: 'echo' | 'tools',
): string[] {
  const args = ['-m', 'langstage_vscode', '--workspace', workspace];
  if (demo) {
    args.push(demo === 'echo' ? '--demo' : `--demo=${demo}`);
  } else if (agentSpec) {
    args.push('--agent', agentSpec);
  }
  return args;
}

/** The environment the sidecar runs with: the workspace root under both names. */
export function sidecarEnv(workspace: string, base: NodeJS.ProcessEnv = process.env): NodeJS.ProcessEnv {
  return {
    ...base,
    LANGSTAGE_WORKSPACE_ROOT: workspace,
    // Older sidecar versions read the legacy name.
    DEEPAGENT_WORKSPACE_ROOT: workspace,
  };
}

/** The startup error the sidecar reports when no agent is configured (gh #121/#131). */
export function isNoAgentError(error: string | undefined): boolean {
  return !!error && /^no agent spec\b/i.test(error.trim());
}

interface QueuedTurn {
  sessionId: string;
  command: Record<string, unknown>;
  handlers: TurnHandlers;
  resolve: (r: TurnResult) => void;
  reject: (e: Error) => void;
  result: TurnResult;
}

export class SidecarClient {
  private proc: ChildProcess | undefined;
  private rl: readline.Interface | undefined;
  private readyPromise: Promise<void> | undefined;
  private readySettled = false;
  private startupError: string | undefined;
  private stderrTail = '';
  private active: QueuedTurn | undefined;
  private queue: QueuedTurn[] = [];
  private _status: SidecarStatus = { state: 'idle' };
  private disposed = false;
  private readonly events = new EventEmitter();

  constructor(private readonly options: SidecarOptions) {}

  get status(): SidecarStatus {
    return { ...this._status };
  }

  /** True while the process is starting or ready (not failed, stopped or disposed). */
  get alive(): boolean {
    return this._status.state === 'starting' || this._status.state === 'ready';
  }

  /** The session whose turn is streaming now, if any. */
  get activeSessionId(): string | undefined {
    return this.active?.sessionId;
  }

  /** Session ids waiting behind the active turn, oldest first. */
  get queuedSessionIds(): string[] {
    return this.queue.map((t) => t.sessionId);
  }

  onStatus(listener: (status: SidecarStatus) => void): { dispose(): void } {
    this.events.on('status', listener);
    return { dispose: () => this.events.off('status', listener) };
  }

  /** A frame that arrived with no turn in flight (e.g. an `error` for a stray cancel). */
  onStrayFrame(listener: (frame: SidecarFrame) => void): { dispose(): void } {
    this.events.on('stray', listener);
    return { dispose: () => this.events.off('stray', listener) };
  }

  /** Spawn the process if needed. Resolves on `ready`; rejects with the startup error. */
  start(): Promise<void> {
    if (this.disposed) return Promise.reject(new Error('sidecar is disposed'));
    if (this.readyPromise) return this.readyPromise;
    this.readyPromise = this.spawnProcess();
    // A start() nobody awaits must not surface as an unhandled rejection.
    this.readyPromise.catch(() => undefined);
    return this.readyPromise;
  }

  /**
   * Queue a `message` or `decision` command for `sessionId`. It is written once the
   * sidecar is ready and every earlier turn has ended.
   */
  runTurn(sessionId: string, command: Record<string, unknown>, handlers: TurnHandlers = {}): Turn {
    const done = new Promise<TurnResult>((resolve, reject) => {
      const turn: QueuedTurn = {
        sessionId,
        command: { ...command, session_id: sessionId },
        handlers,
        resolve,
        reject,
        result: { acked: false },
      };
      if (this.disposed) {
        reject(new Error('sidecar is disposed'));
        return;
      }
      this.queue.push(turn);
      this.start().then(
        () =>
          this._status.state === 'ready'
            ? this.pump()
            : this.failAll(new Error('sidecar is not available')),
        (err: Error) => this.failAll(err),
      );
    });
    return { sessionId, done };
  }

  /**
   * Stop `sessionId`'s turn. The active turn gets a cooperative `cancel` (the sidecar
   * answers `cancelled → turn_end` and keeps the session's memory); a queued turn is
   * dropped before it reaches the sidecar. Returns false if the session has no turn.
   */
  cancel(sessionId: string): boolean {
    const idx = this.queue.findIndex((t) => t.sessionId === sessionId);
    if (idx >= 0) {
      const [turn] = this.queue.splice(idx, 1);
      turn.resolve({ acked: false, cancelledBeforeStart: true });
      return true;
    }
    if (this.active?.sessionId !== sessionId) return false;
    if (!this.write({ type: 'cancel', session_id: sessionId })) {
      // stdin is gone (the process is dying): end the turn here; the next one respawns.
      const turn = this.active;
      this.active = undefined;
      turn.resolve(turn.result);
      this.dispose();
    }
    return true;
  }

  /** Kill the process and fail anything pending. Safe to call repeatedly. */
  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.failAll(new Error('sidecar was stopped'));
    try {
      this.rl?.close();
    } catch {
      /* ignore */
    }
    try {
      this.proc?.stdin?.write(JSON.stringify({ type: 'shutdown' }) + '\n');
    } catch {
      /* ignore */
    }
    try {
      this.proc?.kill();
    } catch {
      /* ignore */
    }
    if (this._status.state !== 'failed') this.setStatus({ state: 'stopped' });
    this.events.removeAllListeners();
  }

  // ------------------------------------------------------------------ internals

  private setStatus(status: SidecarStatus): void {
    this._status = status;
    this.events.emit('status', this.status);
  }

  private write(command: Record<string, unknown>): boolean {
    const stdin = this.proc?.stdin;
    if (!stdin || stdin.destroyed || !stdin.writable) return false;
    try {
      stdin.write(JSON.stringify(command) + '\n');
      return true;
    } catch {
      return false;
    }
  }

  private pump(): void {
    if (this.active || this.disposed || this._status.state !== 'ready') return;
    const next = this.queue.shift();
    if (!next) return;
    this.active = next;
    if (!this.write(next.command)) {
      this.active = undefined;
      next.reject(new Error('sidecar is not available'));
      this.pump();
      return;
    }
    next.handlers.onStart?.();
  }

  private failAll(err: Error): void {
    const pending = [...(this.active ? [this.active] : []), ...this.queue];
    this.active = undefined;
    this.queue = [];
    for (const t of pending) t.reject(err);
  }

  private spawnProcess(): Promise<void> {
    const { python, args, cwd } = this.options;
    const spawnFn = this.options.spawn ?? (nodeSpawn as SpawnFn);
    this.setStatus({ state: 'starting' });

    return new Promise<void>((resolve, reject) => {
      const settle = (err?: Error) => {
        if (this.readySettled) return;
        this.readySettled = true;
        if (err) {
          this.setStatus({ state: 'failed', error: err.message, stderrTail: this.stderrTail || undefined });
          reject(err);
        } else {
          this.setStatus({ state: 'ready' });
          resolve();
        }
      };

      let proc: ChildProcess;
      try {
        // cwd anchors the sidecar's langstage.toml walk-up at the workspace.
        proc = spawnFn(python, args, { cwd, env: this.options.env ?? process.env });
      } catch (e) {
        settle(e instanceof Error ? e : new Error(String(e)));
        return;
      }
      this.proc = proc;
      if (!proc.stdout || !proc.stdin) {
        settle(new Error('sidecar has no stdio pipes'));
        return;
      }

      // Reading stderr also keeps a chatty agent from filling the pipe and blocking.
      proc.stderr?.on('data', (chunk: Buffer | string) => {
        this.stderrTail = (this.stderrTail + chunk.toString()).slice(-4000);
      });

      const notReady = (code: number | null) => {
        if (this.startupError) {
          settle(new Error(this.startupError));
          return;
        }
        const lastLine = this.stderrTail.trim().split(/\r?\n/).pop()?.trim();
        const detail = lastLine ? `: ${lastLine}` : code !== null ? ` (exit code ${code})` : '';
        settle(new Error(`sidecar exited before it was ready${detail}`));
      };

      this.rl = readline.createInterface({ input: proc.stdout });
      this.rl.on('line', (line: string) => {
        const text = line.trim();
        if (!text) return;
        let frame: SidecarFrame;
        try {
          frame = JSON.parse(text) as SidecarFrame;
        } catch {
          return; // non-JSON noise
        }
        if (!frame || typeof frame !== 'object' || typeof frame.type !== 'string') return;
        // `ready` comes exactly once, at startup, and gates the first turn.
        if (frame.type === 'ready') {
          settle();
          this.pump();
          return;
        }
        // gh #131: a startup failure is an `error` frame BEFORE `ready`, then exit.
        if (!this.readySettled && frame.type === 'error') {
          if (this.startupError === undefined) this.startupError = String(frame.error ?? 'unknown error');
          return;
        }
        this.route(frame);
      });

      proc.on('error', (err: Error) => {
        settle(err);
        this.onProcessGone(err);
      });
      // `close` fires after stdout is fully read, so a pre-`ready` error frame has been
      // seen by then. `exit` can come first; give `close` a moment before settling.
      proc.on('close', (code: number | null) => {
        notReady(code);
        this.onProcessGone(new Error('sidecar exited mid-turn'));
      });
      proc.on('exit', (code: number | null) => {
        setTimeout(() => {
          notReady(code);
          this.onProcessGone(new Error('sidecar exited mid-turn'));
        }, this.options.exitGraceMs ?? 500);
      });
    });
  }

  private onProcessGone(err: Error): void {
    if (this._status.state === 'failed') {
      // It never became ready: pending turns get the startup error, not "mid-turn".
      this.failAll(new Error(this._status.error ?? err.message));
      return;
    }
    if (this._status.state === 'ready') {
      this.setStatus({ state: 'stopped', stderrTail: this.stderrTail || undefined });
    }
    this.failAll(err);
  }

  private route(frame: SidecarFrame): void {
    const turn = this.active;
    if (!turn) {
      this.events.emit('stray', frame);
      return;
    }
    if (frame.type === 'ack') turn.result.acked = true;
    if (frame.type === 'interrupt') turn.result.interrupt = frame;
    try {
      turn.handlers.onFrame?.(frame);
    } catch {
      /* a rendering bug must not wedge the queue */
    }
    if (frame.type === 'turn_end') {
      this.active = undefined;
      turn.resolve(turn.result);
      this.pump();
    }
  }
}
