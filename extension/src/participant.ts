import * as vscode from 'vscode';
import { randomUUID } from 'crypto';
import { readLaunchConfig } from './config';
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
import { SidecarClient, SidecarFrame, sidecarArgs, sidecarEnv } from './sidecar';

/**
 * The `@langstage` chat participant, for Copilot users (ADR 0001). Registered only
 * where the editor provides `vscode.chat` (see extension.ts).
 *
 * Each turn is bridged to the Python sidecar through this surface's own
 * `SidecarClient`. The sidecar is **long-lived**: one process is spawned on the first
 * `@langstage` message of a conversation and reused for every later turn, so an
 * in-process checkpointer (`MemorySaver`) keeps the LangGraph thread alive across
 * turns (gh #54). It restarts on a config change, when a new conversation begins, and
 * when the extension unloads. The panel owns a separate client, so this restart never
 * touches the panel's conversations.
 */

type AgentEvent = SidecarFrame;

/** The participant's sidecar, and the config it was started with. */
let client: { sc: SidecarClient; key: string } | null = null;

/** Command behind the interrupt card's buttons. */
export const ANSWER_COMMAND = 'langstage.answerInterrupt';

/** ChatResult.metadata key carrying a conversation's sidecar `session_id`. */
const SESSION_KEY = 'langstageSessionId';
/** ChatResult.metadata key carrying the interrupt a turn ended on, if any. */
const PENDING_KEY = 'langstagePendingInterrupt';

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

export async function handler(
  request: vscode.ChatRequest,
  chatContext: vscode.ChatContext,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<vscode.ChatResult> {
  // Returned on every path so the NEXT turn of this conversation finds its id (gh #133).
  const sessionId = conversationSessionId(chatContext.history);
  const metadata: Record<string, unknown> = { [SESSION_KEY]: sessionId };
  const result: vscode.ChatResult = { metadata };

  const { python, agentSpec, workspace } = readLaunchConfig();

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

  // Memory isolation between conversations is the per-conversation `sessionId`
  // above (gh #133) — NOT this restart, which only fires on a conversation's first
  // turn. A new chat still starts a clean process (dropping the previous chat's
  // in-process state); subsequent turns reuse it (that reuse is the gh #54 fix).
  if (chatContext.history.length === 0) {
    disposeParticipantSidecar();
  }

  try {
    const sc = getOrCreateClient(python, agentSpec, workspace);
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
    disposeParticipantSidecar();
    stream.markdown(`\n\n❌ ${err instanceof Error ? err.message : String(err)}`);
  }
  return result;
}

/** Reuse the live client if its config matches; otherwise (re)spawn one. */
function getOrCreateClient(python: string, agentSpec: string, workspace: string): SidecarClient {
  const key = [python, agentSpec, workspace].join('|');
  if (client && client.sc.alive && client.key === key) {
    return client.sc;
  }
  disposeParticipantSidecar(); // config changed or process is gone
  const sc = new SidecarClient({
    python,
    args: sidecarArgs(workspace, agentSpec),
    cwd: workspace,
    env: sidecarEnv(workspace),
  });
  client = { sc, key };
  return sc;
}

/** Kill and forget the participant's sidecar (if any). Safe to call repeatedly. */
export function disposeParticipantSidecar(): void {
  const c = client;
  client = null;
  c?.sc.dispose();
}

/**
 * Send one command to the (persistent) sidecar and pump its frames into the chat
 * stream until the turn ends. The conversation's `sessionId` maps to a stable
 * LangGraph `thread_id`, so a checkpointer-backed agent remembers prior turns
 * (gh #54), and only this conversation's turns (gh #133).
 */
async function runTurn(
  sc: SidecarClient,
  sessionId: string,
  command: Record<string, unknown>,
  stream: vscode.ChatResponseStream,
  token: vscode.CancellationToken,
): Promise<TurnRenderState> {
  const render: TurnRenderState = { lastMessageId: undefined, interrupt: undefined, acked: false };
  // The sidecar's cooperative `cancel` (gh #67) aborts just the in-flight turn and keeps
  // the process, its session and its checkpointer alive, so memory survives a stop
  // (gh #69). The sidecar answers `cancelled` then `turn_end`, which ends the turn.
  const cancelSub = token.onCancellationRequested(() => sc.cancel(sessionId));
  try {
    const turn = sc.runTurn(sessionId, command, {
      onFrame: (event) => dispatch(event, stream, render),
    });
    const result = await turn.done;
    render.acked = result.acked;
    return render;
  } finally {
    cancelSub.dispose();
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
