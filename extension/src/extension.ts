import * as vscode from 'vscode';
import { spawn, ChildProcess } from 'child_process';
import * as readline from 'readline';
import { PassThrough } from 'stream';

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

async function handler(
  request: vscode.ChatRequest,
  chatContext: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  const config = vscode.workspace.getConfiguration('langstage');
  const legacy = vscode.workspace.getConfiguration('deepagent');
  const agentSpec =
    config.get<string>('agentSpec') || legacy.get<string>('agentSpec') || '';
  const python =
    config.get<string>('pythonPath') || legacy.get<string>('pythonPath') || 'python';

  const workspace =
    vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();

  // A fresh conversation (empty history on its first turn) must not inherit the
  // previous conversation's thread state. The sidecar keys memory off a single
  // `session_id`, so scope memory to a conversation by starting a clean process
  // when a new one begins; subsequent turns reuse it (that reuse is the gh #54 fix).
  if (chatContext.history.length === 0) {
    disposeSidecar();
  }

  try {
    const sc = getOrCreateSidecar(python, agentSpec, workspace);
    await runTurn(sc, request.prompt, stream, token);
  } catch (err) {
    // A broken sidecar must not stay cached and poison every later turn.
    disposeSidecar();
    stream.markdown(`\n\n❌ ${err instanceof Error ? err.message : String(err)}`);
  }
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
    sc.onEvent?.(event);
  });

  proc.on('error', (err: Error) => {
    sc.alive = false;
    settleReady(() => readyReject(err));
  });
  proc.on('exit', () => {
    sc.alive = false;
    settleReady(() => readyReject(new Error('sidecar exited before it was ready')));
    // Drop it if it is still the active sidecar, so the next turn respawns.
    if (sidecar === sc) {
      sidecar = null;
    }
  });

  return sc;
}

/**
 * Send one user message to the (persistent) sidecar and pump its events into the
 * chat stream until the turn ends. The same `session_id` across turns maps to a
 * stable LangGraph `thread_id`, so a checkpointer-backed agent remembers prior
 * turns (gh #54).
 */
function runTurn(
  sc: Sidecar,
  prompt: string,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<void> {
  return sc.ready.then(
    () =>
      new Promise<void>((resolve, reject) => {
        if (!sc.alive || !sc.proc.stdin) {
          reject(new Error('sidecar is not available'));
          return;
        }

        let done = false;
        const exitHandler = () => finish(() => reject(new Error('sidecar exited mid-turn')));
        const cancelSub = token.onCancellationRequested(() => {
          // The stdio protocol has no per-turn cancel command, so stop the turn
          // by killing the process; the next turn respawns a fresh one (memory
          // resets on an explicit cancel). End this turn quietly — the user asked.
          disposeSidecar();
          finish(resolve);
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
          dispatch(event, stream);
          if (event.type === 'turn_end') {
            finish(resolve);
          }
        };
        sc.proc.once('exit', exitHandler);

        sc.proc.stdin.write(
          JSON.stringify({ type: 'message', session_id: 'vscode', content: prompt }) + '\n',
        );
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

/** Map one sidecar event onto the chat response stream. */
function dispatch(event: AgentEvent, stream: vscode.ChatResponseStream): void {
  switch (event.type) {
    case 'content':
      stream.markdown(String(event.content ?? ''));
      break;
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
      // HITL: v0 surfaces the requested action. The decision round-trip
      // (sending {"type":"decision",...} back to the sidecar) is the next
      // increment — it needs a confirmation affordance in the chat UI.
      const actions = (event.action_requests as Array<Record<string, unknown>>) ?? [];
      // The runtime emits the standard HumanInterrupt action_request shape —
      // {"action": <tool>, "args": {...}} — so read `.action`. Fall back to `.tool`
      // for a keyed-dict interrupt, then to a generic label (gh #44).
      const first = actions[0] ?? {};
      const tool = first.action ?? first.tool ?? 'an action';
      stream.markdown(
        `\n\n⚠️ The agent wants to run **${String(tool)}** and is waiting for ` +
          'approval. Interactive approval is not wired up yet in this build.\n',
      );
      break;
    }
    case 'error':
      stream.markdown(`\n\n❌ ${String(event.error ?? 'unknown error')}\n`);
      break;
    // ready / ack / complete / turn_end / usage: no direct UI output.
    default:
      break;
  }
}
