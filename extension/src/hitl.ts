/**
 * Human-in-the-loop (HITL) answers from the chat panel.
 *
 * A turn that ends on an `interrupt` frame pauses the agent until a `decision` command
 * answers it. The chat answers with a slash command on the SAME conversation:
 * `@langstage /approve`, `/reject [reason]`, `/respond <text>` or `/edit <json>`.
 * The interrupt card offers a button per verb the frame's `allowed_decisions` lists.
 *
 * This module is plain logic with no `vscode` import, so it runs under `node --test`.
 * The sidecar stays the authority: it checks every decision against the pending
 * interrupt with langstage-core's `normalize_decision` and refuses one it doesn't allow.
 */

/** The chat slash commands that answer an interrupt, one per canonical verb. */
export const DECISION_COMMANDS = ['approve', 'reject', 'respond', 'edit'] as const;
export type DecisionCommand = (typeof DECISION_COMMANDS)[number];

/** Legacy HumanInterrupt spellings core accepts for the canonical verbs. */
const ALIASES: Record<string, DecisionCommand> = {
  accept: 'approve',
  ignore: 'reject',
  response: 'respond',
};

export function isDecisionCommand(command: string | undefined): command is DecisionCommand {
  return command !== undefined && (DECISION_COMMANDS as readonly string[]).includes(command);
}

/** The canonical verb an advertised decision stands for, or undefined for a custom one. */
export function canonicalVerb(verb: string): DecisionCommand | undefined {
  const lower = verb.toLowerCase();
  if (isDecisionCommand(lower)) return lower;
  return ALIASES[lower];
}

/** What a pending interrupt needs to be answered from a later chat turn. */
export interface PendingInterrupt {
  /** The frame's `allowed_decisions`, spelled as the frame spells them. */
  allowed: string[];
  /** How many actions it asks about: the HITL middleware wants one decision each. */
  actionCount: number;
}

export function pendingFromFrame(frame: Record<string, unknown>): PendingInterrupt {
  const allowed = Array.isArray(frame.allowed_decisions)
    ? frame.allowed_decisions.map((d) => String(d))
    : [];
  const actions = Array.isArray(frame.action_requests) ? frame.action_requests : [];
  return { allowed, actionCount: Math.max(1, actions.length) };
}

/** Read a pending interrupt back out of a turn's result metadata. */
export function pendingFromMetadata(value: unknown): PendingInterrupt | undefined {
  if (!value || typeof value !== 'object') return undefined;
  const v = value as Record<string, unknown>;
  if (!Array.isArray(v.allowed)) return undefined;
  const count = typeof v.actionCount === 'number' && v.actionCount > 0 ? v.actionCount : 1;
  return { allowed: v.allowed.map((d) => String(d)), actionCount: count };
}

export type BuildResult =
  | { ok: true; decisions: Array<Record<string, unknown>> }
  | { ok: false; reason: string };

/**
 * Turn a chat slash command plus its text into the `decisions` list for the sidecar.
 *
 * - the verb is sent in the frame's own spelling (`accept` if the frame lists `accept`);
 * - `approve` takes no text; `respond` needs text; `reject` takes an optional reason;
 *   `edit` needs a JSON object (e.g. `{"edited_action": {"name": ..., "args": {...}}}`),
 *   merged into the decision. Text that is a JSON object is merged for any verb, as the
 *   sidecar's `--repl` does, so every typed decision shape is reachable;
 * - one decision per action the interrupt asks about, all the same.
 */
export function buildDecisions(
  command: DecisionCommand,
  text: string,
  pending: PendingInterrupt,
): BuildResult {
  const verb = pending.allowed.find((a) => canonicalVerb(a) === command);
  if (verb === undefined) {
    const offered = pending.allowed.length ? pending.allowed.join(', ') : 'none';
    return {
      ok: false,
      reason: `The agent's pending request does not accept \`${command}\` (it allows: ${offered}).`,
    };
  }
  const payload = text.trim();
  let decision: Record<string, unknown>;
  if (command === 'approve' && payload) {
    return { ok: false, reason: '`/approve` takes no text. Use `/respond <text>` to answer in words.' };
  }
  if ((command === 'respond' || command === 'edit') && !payload) {
    const usage = command === 'respond' ? '/respond <your answer>' : '/edit {"edited_action": {...}}';
    return { ok: false, reason: `\`/${command}\` needs text: \`${usage}\`.` };
  }
  if (!payload) {
    decision = { type: verb };
  } else if (command === 'edit' || payload.startsWith('{')) {
    let obj: unknown;
    try {
      obj = JSON.parse(payload);
    } catch (err) {
      return { ok: false, reason: `\`/${command}\` text is not valid JSON: ${(err as Error).message}` };
    }
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
      return { ok: false, reason: `\`/${command}\` text must be a JSON object.` };
    }
    decision = { ...(obj as Record<string, unknown>), type: verb };
  } else {
    decision = { type: verb, message: payload };
  }
  return { ok: true, decisions: Array.from({ length: pending.actionCount }, () => ({ ...decision })) };
}

/** One row of the interrupt card: the action the agent wants to take. */
export interface ActionSummary {
  name: string;
  description?: string;
  args?: unknown;
}

/**
 * Read the requested actions out of an interrupt frame. Three shapes reach the wire:
 * HumanInTheLoopMiddleware's `{"name", "args", "description"}`; a HumanInterrupt LIST
 * unwrapped to `{"action", "args"}` (gh #44); and a single `interrupt({...})` object
 * nested as `{"action_request": {"action", ...}, "description"}` (gh #101). A keyed-dict
 * interrupt may name a `tool`.
 */
export function summarizeActions(frame: Record<string, unknown>): ActionSummary[] {
  const requests = Array.isArray(frame.action_requests) ? frame.action_requests : [];
  const out: ActionSummary[] = [];
  for (const raw of requests) {
    if (!raw || typeof raw !== 'object') continue;
    const req = raw as Record<string, unknown>;
    const nested =
      req.action_request && typeof req.action_request === 'object'
        ? (req.action_request as Record<string, unknown>)
        : undefined;
    const src = nested ?? req;
    const name = src.name ?? src.action ?? req.tool ?? 'an action';
    const description = req.description ?? src.description;
    out.push({
      name: String(name),
      description: typeof description === 'string' && description ? description : undefined,
      args: src.args,
    });
  }
  return out.length ? out : [{ name: 'an action' }];
}

/** Button label for an advertised verb. */
export function buttonTitle(verb: string): string {
  const canon = canonicalVerb(verb);
  switch (canon) {
    case 'approve':
      return 'Approve';
    case 'reject':
      return 'Reject';
    case 'respond':
      return 'Respond…';
    case 'edit':
      return 'Edit…';
    default:
      return verb;
  }
}

/**
 * The chat query a button submits. `approve` / `reject` send straight away; `respond`
 * and `edit` need the user's text, so they only prefill the input.
 */
export function buttonQuery(verb: string): { query: string; isPartialQuery: boolean } | undefined {
  const canon = canonicalVerb(verb);
  if (!canon) return undefined;
  const needsText = canon === 'respond' || canon === 'edit';
  return { query: `@langstage /${canon}${needsText ? ' ' : ''}`, isPartialQuery: needsText };
}
