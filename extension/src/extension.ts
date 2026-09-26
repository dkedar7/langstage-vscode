import * as vscode from 'vscode';
import { spawn, ChildProcess } from 'child_process';
import { randomUUID } from 'crypto';
import * as readline from 'readline';
import { PassThrough } from 'stream';
import {
  PendingInterrupt,
  buildDecisions,
  buttonQuery,
  buttonTitle,
  isDecisionCommand,
  pendingFromFrame,
  pendingFromMetadata,
  summarizeActions,
} from './hitl';

/**
 * LangStage VS Code chat participant.
 *
 * Registers `@langstage` in the chat panel (alongside Copilot) and bridges each
 * turn to the Python `langstage-vscode` sidecar over stdio. The sidecar emits
 * newline-delimited JSON events — the langstage-core `event_to_dict()`
 * wire vocabulary — which the dispatcher below maps onto the chat response.
 *
 * The sidecar is **long-lived**: one process is spawned on the first `@langstage`
 * message of a conversation and reused for every subsequent turn, so an
 * in-process checkpointer (`MemorySaver`) keeps the LangGraph thread alive across
 * turns and the documented multi-turn "conversational memory" actually holds
 * (gh #54). It restarts on a config change, when a new conversation begins, and
 * when the extension unloads. (Previously the extension spawned a fresh process
 * per message and killed it after one turn, so any in-process checkpointer was
 * wiped between turns — the agent had amnesia on turn 2.)
 */
export function activate(context: vscode.ExtensionContext) {
  const participant = vscode.chat.createChatParticipant('langstage.agent', handler);
  participant.iconPath = new vscode.ThemeIcon('robot');
  context.subscriptions.push(participant);

  // The interrupt card's buttons: submit (or prefill) `@langstage /<verb>` in the chat,
  // so the answer runs as a turn of the same conversation and streams its reply there.
  context.subscriptions.push(
    vscode.commands.registerCommand(
      ANSWER_COMMAND,
      (q: { query: string; isPartialQuery: boolean }) =>
        vscode.commands.executeCommand('workbench.action.chat.open', q),
    ),
  );

  // Tear the sidecar down when the extension unloads.
  context.subscriptions.push({ dispose: () => disposeSidecar() });

  // A changed interpreter / agent spec must not keep serving from a stale
  // long-lived process — drop it so the next turn respawns with the new config.
  context.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('langstage') || e.affectsConfiguration('deepagent')) {
        disposeSidecar();
      }
    }),
  );
}

export function deactivate() {
  disposeSidecar();
}

interface AgentEvent {
  type: string;
  [key: string]: unknown;
}

/**
 * A long-lived sidecar process shared across the turns of one chat conversation.
 * `onEvent` is the current turn's consumer — set while a turn is streaming and
 * cleared when it ends — so a single persistent readline can route each frame to
 * whichever turn is in flight (chat turns are serialized by VS Code).
 */
interface Sidecar {
  proc: ChildProcess;
  rl: readline.Interface;
  ready: Promise<void>;
  key: string; // config identity: pythonPath | agentSpec | workspace
  alive: boolean;
  onEvent: ((event: AgentEvent) => void) | null;
}

// Module-scoped so it survives across `handler` invocations (turns).
let sidecar: Sidecar | null = null;

function sidecarKey(python: string, agentSpec: string, workspace: string): string {
  return [python, agentSpec, workspace].join('|');
}

/** ChatResult.metadata key carrying a conversation's sidecar `session_id`. */
const SESSION_KEY = 'langstageSessionId';
/** ChatResult.metadata key carrying the interrupt a turn ended on, if any. */
const PENDING_KEY = 'langstagePendingInterrupt';
/** Command behind the interrupt card's buttons. */
const ANSWER_COMMAND = 'langstage.answerInterrupt';

/**
 * The interrupt this conversation's latest @langstage turn ended on, if any. A turn
 * that ends paused returns it in its result metadata, the same way the session id
 * travels (gh #133); the turn that answers it returns none, so it clears.
 */
function pendingInterrupt(history: vscode.ChatContext['history']): PendingInterrupt | undefined {
  for (let i = history.length - 1; i >= 0; i--) {
    const turn = history[i];
    if (turn instanceof vscode.ChatResponseTurn) {
      return pendingFromMetadata(turn.result.metadata?.[PENDING_KEY]);
    }
  }
  return undefined;
}

/**
 * The `session_id` (hence LangGraph `thread_id`) for THIS chat conversation (gh #133).
 *
 * Every conversation used to share the constant `session_id: 'vscode'`, and the only
 * isolation was a process restart on a conversation's first turn — which never fires
 * when you RESUME an earlier chat. So resuming chat A after using chat B reused B's warm
 * process and landed A's follow-up on B's thread: A saw B's messages (and with a durable
 * checkpointer, every chat ever shared one thread across restarts too).
 *
 * VS Code exposes no conversation id, but it does replay each turn's `ChatResult` in
 * `chatContext.history`. So a conversation's first turn mints a random id and returns it
 * in the result metadata; every later turn reads it back from the newest response turn
 * that carries it. A conversation with no id in its history (e.g. one begun before this
 * fix) gets a FRESH id rather than a derived one: a content-derived id could collide
 * between two chats that start with the same prompt — worst case here is a clean thread,
 * never another chat's memory.
 */
function conversationSessionId(history: vscode.ChatContext['history']): string {
  for (let i = history.length - 1; i >= 0; i--) {
    const turn = history[i];
    if (turn instanceof vscode.ChatResponseTurn) {
      const id: unknown = turn.result.metadata?.[SESSION_KEY];
      if (typeof id === 'string' && id) {
        return id;
      }
    }
  }
  return `vscode-${randomUUID()}`;
}

async function handler(
  request: vscode.ChatRequest,
  chatContext: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<vscode.ChatResult> {
  // Returned on every path so the NEXT turn of this conversation finds its id (gh #133).
  const sessionId = conversationSessionId(chatContext.history);
  const metadata: Record<string, unknown> = { [SESSION_KEY]: sessionId };
  const result: vscode.ChatResult = { metadata };

  const config = vscode.workspace.getConfiguration('langstage');
  const legacy = vscode.workspace.getConfiguration('deepagent');
  const agentSpec =
    config.get<string>('agentSpec') || legacy.get<string>('agentSpec') || '';
  const python =
    config.get<string>('pythonPath') || legacy.get<string>('pythonPath') || 'python';

  // HITL: `/approve`, `/reject`, `/respond`, `/edit` answer the interrupt this
  // conversation is paused on, as a `decision` on the same session, and the resumed
  // turn streams into this response. A plain message can't reach a paused agent (the
  // sidecar refuses it, gh #134), so it gets the ways to answer instead.
  const pending = pendingInterrupt(chatContext.history);
  let command: Record<string, unknown>;
  if (isDecisionCommand(request.command)) {
    if (!pending) {
      stream.markdown('Nothing in this conversation is waiting for a decision.');
      return result;
    }
    const built = buildDecisions(request.command, request.prompt, pending);
    if (!built.ok) {
      stream.markdown(built.reason);
      renderDecisionButtons(stream, pending.allowed);
      metadata[PENDING_KEY] = pending; // still paused
      return result;
    }
    command = { type: 'decision', session_id: sessionId, decisions: built.decisions };
  } else if (pending) {
    stream.markdown(
      'The agent is paused, waiting for your decision on its last request. Answer it ' +
        'with a button, or type `@langstage /approve`, `/reject [reason]`, ' +
        '`/respond <text>` or `/edit <json>` (whichever it allows).',
    );
    renderDecisionButtons(stream, pending.allowed);
    metadata[PENDING_KEY] = pending;
    return result;
  } else {
    command = { type: 'message', session_id: sessionId, content: request.prompt };
  }

  // gh #118: an empty prompt (`@langstage` + Enter) would reach the sidecar as a
  // `message` with empty `content`, which it rejects with an `error` frame —
  // historically with no `turn_end`, leaving runTurn's promise pending forever
  // and the spinner stuck. Short-circuit here with a hint instead of spawning
  // a doomed turn. (The sidecar now also emits `turn_end` on that rejection.)
  if (command.type === 'message' && !request.prompt.trim()) {
    stream.markdown('Please type a message for @langstage — the prompt was empty.');
    return result;
  }

  const workspace =
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();

  // Memory isolation between conversations is the per-conversation `sessionId`
  // above (gh #133) — NOT this restart, which only fires on a conversation's first
  // turn. A new chat still starts a clean process (dropping the previous chat's
  // in-process state); subsequent turns reuse it (that reuse is the gh #54 fix).
  if (chatContext.history.length === 0) {
    disposeSidecar();
  }

  try {
    const sc = getOrCreateSidecar(python, agentSpec, workspace);
    const turn = await runTurn(sc, sessionId, command, stream, token);
    // A turn that ends on an interrupt leaves the conversation paused. So does a
    // decision the sidecar refused (an error before any `ack`): the interrupt it was
    // answering is still pending there, so keep offering the buttons.
    const refused = command.type === 'decision' && !turn.acked;
    const stillPending = turn.interrupt ?? (refused ? pending : undefined);
    if (stillPending) {
      metadata[PENDING_KEY] = stillPending;
      if (refused) renderDecisionButtons(stream, stillPending.allowed);
    }
  } catch (err) {
    // A broken sidecar must not stay cached and poison every later turn.
    disposeSidecar();
    stream.markdown(`\n\n❌ ${err instanceof Error ? err.message : String(err)}`);
  }
  return result;
}

/** Reuse the live sidecar if its config matches; otherwise (re)spawn one. */
function getOrCreateSidecar(
  python: string,
  agentSpec: string,
  workspace: string,
): Sidecar {
  const key = sidecarKey(python, agentSpec, workspace);
  if (sidecar && sidecar.alive && sidecar.key === key) {
    return sidecar;
  }
  disposeSidecar(); // config changed or process is gone
  sidecar = spawnSidecar(python, agentSpec, workspace, key);
  return sidecar;
}

function spawnSidecar(
  python: string,
  agentSpec: string,
  workspace: string,
  key: string,
): Sidecar {
  const args = ['-m', 'langstage_vscode', '--workspace', workspace];
  if (agentSpec) {
    args.push('--agent', agentSpec);
  }

  const proc = spawn(python, args, {
    // cwd anchors the sidecar's langstage.toml walk-up at the workspace.
    cwd: workspace,
    env: {
      ...process.env,
      LANGSTAGE_WORKSPACE_ROOT: workspace,
      // Older sidecar versions read the legacy name.
      DEEPAGENT_WORKSPACE_ROOT: workspace,
    },
  });

  let readyResolve!: () => void;
  let readyReject!: (err: Error) => void;
  const ready = new Promise<void>((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  let settled = false;
  const settleReady = (fn: () => void) => {
    if (!settled) {
      settled = true;
      fn();
    }
  };

  // spawn() with the default stdio gives us pipes; this guard is defensive.
  const input = proc.stdout ?? new PassThrough();
  const sc: Sidecar = {
    proc,
    rl: readline.createInterface({ input }),
    ready,
    key,
    alive: true,
    onEvent: null,
  };

  if (!proc.stdout || !proc.stdin) {
    sc.alive = false;
    settleReady(() => readyReject(new Error('sidecar has no stdio pipes')));
    return sc;
  }

  // gh #131: a startup failure (no agent spec, a spec that fails to load, an unusable
  // workspace) is reported as an `error` frame BEFORE `ready`, and then the sidecar
  // exits. No turn is listening yet (`onEvent` is set only once `ready` resolves), so
  // that frame used to be dropped and the chat showed a bare "sidecar exited before it
  // was ready". Keep it, plus the tail of stderr for failures that happen before the
  // sidecar can write a frame at all (e.g. the interpreter lacks langstage-vscode).
  let startupError: string | undefined;
  let stderrTail = '';
  proc.stderr?.on('data', (chunk: Buffer | string) => {
    // Reading stderr also keeps a chatty agent from filling the pipe and blocking.
    stderrTail = (stderrTail + chunk.toString()).slice(-4000);
  });
  const rejectNotReady = (code: number | null) => {
    settleReady(() => {
      if (startupError) {
        readyReject(new Error(startupError));
        return;
      }
      const lastLine = stderrTail.trim().split(/\r?\n/).pop()?.trim();
      const detail = lastLine ? `: ${lastLine}` : code !== null ? ` (exit code ${code})` : '';
      readyReject(new Error(`sidecar exited before it was ready${detail}`));
    });
  };

  sc.rl.on('line', (line: string) => {
    const text = line.trim();
    if (!text) return;
    let event: AgentEvent;
    try {
      event = JSON.parse(text) as AgentEvent;
    } catch {
      return; // ignore non-JSON noise
    }
    // The sidecar emits `ready` exactly once, at startup — it gates the first
    // turn. Every later turn sends its message immediately (the process is
    // already ready); there is no second `ready` to wait for.
    if (event.type === 'ready') {
      settleReady(readyResolve);
      return;
    }
    if (!settled && event.type === 'error' && startupError === undefined) {
      startupError = String(event.error ?? 'unknown error');
      return;
    }
    sc.onEvent?.(event);
  });

  proc.on('error', (err: Error) => {
    sc.alive = false;
    settleReady(() => readyReject(err));
  });
  // `close` fires after stdout has been fully read, so a pre-`ready` error frame has
  // been seen by then. `exit` can come first; give `close` a moment before settling.
  proc.on('close', (code: number | null) => rejectNotReady(code));
  proc.on('exit', (code: number | null) => {
    sc.alive = false;
    setTimeout(() => rejectNotReady(code), 500);
    // Drop it if it is still the active sidecar, so the next turn respawns.
    if (sidecar === sc) {
      sidecar = null;
    }
  });

  return sc;
}

/**
 * Send one user message to the (persistent) sidecar and pump its events into the
 * chat stream until the turn ends. The conversation's `sessionId` maps to a stable
 * LangGraph `thread_id`, so a checkpointer-backed agent remembers prior turns
 * (gh #54) — and only this conversation's turns (gh #133).
 */
function runTurn(
  sc: Sidecar,
  sessionId: string,
  command: Record<string, unknown>,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<TurnRenderState> {
  return sc.ready.then(
    () =>
      new Promise<TurnRenderState>((resolve, reject) => {
        if (!sc.alive || !sc.proc.stdin) {
          reject(new Error('sidecar is not available'));
          return;
        }

        let done = false;
        const turn: TurnRenderState = {
          lastMessageId: undefined,
          interrupt: undefined,
          acked: false,
        };
        const exitHandler = () => finish(() => reject(new Error('sidecar exited mid-turn')));
        const cancelSub = token.onCancellationRequested(() => {
          // The sidecar has a cooperative per-turn `cancel` command (gh #67,
          // shipped in the 0.5.19 sidecar): abort just the in-flight turn while
          // keeping the process, its session, and its in-process checkpointer
          // (`MemorySaver`) alive, so conversational memory survives a "stop"
          // (gh #69). We deliberately do NOT disposeSidecar() here — killing the
          // process would wipe that memory, the exact harm gh #67 set out to fix.
          // The turn is not resolved here either: the sidecar answers with
          // `cancelled` then `turn_end`, and the normal frame loop below ends the
          // turn on `turn_end` without tearing the process down, so the next turn
          // reuses the same warm process (same sessionId -> same thread_id).
          try {
            sc.proc.stdin?.write(
              JSON.stringify({ type: 'cancel', session_id: sessionId }) + '\n',
            );
          } catch {
            // stdin is already gone (the process is dying) — fall back to a clean
            // teardown so the turn still ends and the next one respawns.
            disposeSidecar();
            finish(() => resolve(turn));
          }
        });

        function finish(settle: () => void): void {
          if (done) return;
          done = true;
          sc.onEvent = null;
          sc.proc.removeListener('exit', exitHandler);
          cancelSub.dispose();
          settle();
        }

        sc.onEvent = (event: AgentEvent) => {
          dispatch(event, stream, turn);
          if (event.type === 'turn_end') {
            finish(() => resolve(turn));
          }
        };
        sc.proc.once('exit', exitHandler);

        // A `message`, or a `decision` answering this session's pending interrupt.
        sc.proc.stdin.write(JSON.stringify(command) + '\n');
      }),
  );
}

/** Kill and forget the current sidecar (if any). Safe to call repeatedly. */
function disposeSidecar(): void {
  const sc = sidecar;
  sidecar = null;
  if (!sc) return;
  sc.alive = false;
  try {
    sc.rl.close();
  } catch {
    /* ignore */
  }
  try {
    sc.proc.kill();
  } catch {
    /* ignore */
  }
}

/** Per-turn rendering state carried between `dispatch` calls. */
interface TurnRenderState {
  /** `message_id` of the last `content` frame rendered in this turn (gh #108). */
  lastMessageId: string | undefined;
  /** The interrupt this turn ended on: the conversation now waits for a decision. */
  interrupt: PendingInterrupt | undefined;
  /** The sidecar accepted the command (`ack`). */
  acked: boolean;
}

/** One button per advertised verb the chat can send; any other verb is listed as text. */
function renderDecisionButtons(stream: vscode.ChatResponseStream, allowed: string[]): void {
  const other: string[] = [];
  for (const verb of allowed) {
    const q = buttonQuery(verb);
    if (q) {
      stream.button({ command: ANSWER_COMMAND, title: buttonTitle(verb), arguments: [q] });
    } else {
      other.push('`' + verb + '`');
    }
  }
  if (other.length) {
    stream.markdown(`\n\nAlso allowed, but not answerable from the chat: ${other.join(', ')}.\n`);
  }
}

/** Map one sidecar event onto the chat response stream. */
function dispatch(
  event: AgentEvent,
  stream: vscode.ChatResponseStream,
  turn: TurnRenderState,
): void {
  switch (event.type) {
    case 'content': {
      // gh #108: every `content` frame carries the `message_id` of the AIMessage it
      // belongs to (langstage-core >= 1.0.36). When it changes, a new assistant
      // message starts (e.g. a planner node, then an answer node), so open a new
      // paragraph instead of gluing the two replies into one line. Token chunks of
      // one message share an id; a frame without one never adds a break.
      const text = String(event.content ?? '');
      const messageId = typeof event.message_id === 'string' ? event.message_id : undefined;
      if (text && messageId !== undefined) {
        if (turn.lastMessageId !== undefined && messageId !== turn.lastMessageId) {
          stream.markdown('\n\n');
        }
        turn.lastMessageId = messageId;
      }
      stream.markdown(text);
      break;
    }
    case 'reasoning':
      stream.markdown(`\n\n*${String(event.content ?? '')}*\n\n`);
      break;
    case 'tool_start':
      stream.progress(`Running \`${String(event.name ?? 'tool')}\`…`);
      break;
    case 'tool_end': {
      const status = event.status === 'error' ? '❌' : '✓';
      stream.markdown(`\n\n${status} \`${String(event.name ?? 'tool')}\`\n`);
      break;
    }
    case 'extraction':
      if (event.extracted_type === 'todos' && Array.isArray(event.data)) {
        stream.markdown('\n\n**Tasks**\n');
        for (const item of event.data as Array<Record<string, unknown>>) {
          const done = item.status === 'completed';
          const content = String(item.content ?? item.task ?? '');
          stream.markdown(`- ${done ? '[x]' : '[ ]'} ${content}\n`);
        }
      }
      break;
    case 'interrupt': {
      // HITL: show what the agent wants to do and a button per verb the frame's
      // `allowed_decisions` lists. A button submits `@langstage /<verb>` in this chat;
      // the handler turns that into a `decision` on the same session (see hitl.ts).
      const pending = pendingFromFrame(event);
      turn.interrupt = pending;
      const lines = ['\n\n⚠️ **The agent is waiting for your decision.**\n'];
      for (const action of summarizeActions(event)) {
        lines.push(`\n- **${action.name}**${action.description ? `: ${action.description}` : ''}\n`);
        if (action.args !== undefined) {
          lines.push('\n  ```json\n  ' + JSON.stringify(action.args, null, 2).replace(/\n/g, '\n  ') + '\n  ```\n');
        }
      }
      if (!pending.allowed.length) {
        lines.push('\nThe request lists no decisions the chat can send.\n');
      }
      stream.markdown(lines.join(''));
      renderDecisionButtons(stream, pending.allowed);
      break;
    }
    case 'error':
      stream.markdown(`\n\n❌ ${String(event.error ?? 'unknown error')}\n`);
      break;
    case 'ack':
      turn.acked = true;
      break;
    // ready / ack / complete / cancelled / turn_end / usage: no direct UI
    // output. `cancelled` (gh #67/#69) is the terminal frame for a cooperatively
    // stopped turn — it must fall through silently here (not be treated as an
    // error or unknown frame); the paired `turn_end` ends the turn in runTurn().
    default:
      break;
  }
}
