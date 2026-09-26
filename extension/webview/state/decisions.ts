/**
 * The approval card's decisions (M3), built with hitl.ts `buildDecisions`, unchanged:
 * the same verb matching (the frame's own spelling, so `accept` stays `accept`),
 * aliases (accept → approve, ignore → reject, response → respond, as core's
 * `normalize_decision`) and payload rules the `@langstage` chat participant uses. The
 * sidecar stays the authority and validates every verb against the pending interrupt.
 *
 * No DOM or React imports, so it runs under `node --test`.
 */
import {
  BuildResult,
  DecisionCommand,
  PendingInterrupt,
  buildDecisions,
  canonicalVerb,
  pendingFromFrame,
  summarizeActions,
} from '../../src/hitl';
import type { Frame } from '../../src/shared/panelProtocol';

/** One button on the card: a verb as the frame spells it, and what it means. */
export interface CardVerb {
  verb: string;
  /** undefined for a custom verb the graph advertises itself. */
  canon: DecisionCommand | undefined;
}

const ORDER: Record<DecisionCommand, number> = { approve: 0, edit: 1, respond: 2, reject: 3 };

/** The card's buttons: one per `allowed_decisions` entry (duplicates by meaning dropped). */
export function cardVerbs(frame: Frame): CardVerb[] {
  const seen = new Set<string>();
  const out: CardVerb[] = [];
  for (const verb of pendingFromFrame(frame).allowed) {
    const canon = canonicalVerb(verb.trim());
    const key = canon ?? verb;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({ verb, canon });
  }
  return out.sort((a, b) => (a.canon ? ORDER[a.canon] : 9) - (b.canon ? ORDER[b.canon] : 9));
}

/** Approve, reject (optional reason) or respond (text): the same answer for every action. */
export function simpleDecisions(
  frame: Frame,
  command: 'approve' | 'reject' | 'respond',
  text = '',
): BuildResult {
  return buildDecisions(command, text, pendingFromFrame(frame));
}

/**
 * Edit one action's arguments. `argsJson` is what the user typed; it must be a JSON
 * object. The edited action gets `{type: edit, edited_action: {name, args}}` (the
 * HumanInTheLoopMiddleware shape); any other action is approved as it is, or, if the
 * interrupt doesn't allow approve, "edited" with its own arguments unchanged.
 */
export function editDecisions(frame: Frame, index: number, argsJson: string): BuildResult {
  let args: unknown;
  try {
    args = JSON.parse(argsJson);
  } catch (err) {
    return { ok: false, reason: `The arguments are not valid JSON: ${(err as Error).message}` };
  }
  if (!args || typeof args !== 'object' || Array.isArray(args)) {
    return { ok: false, reason: 'The arguments must be a JSON object.' };
  }
  const pending = pendingFromFrame(frame);
  const one: PendingInterrupt = { allowed: pending.allowed, actionCount: 1 };
  const actions = summarizeActions(frame);
  const decisions: Array<Record<string, unknown>> = [];
  for (let i = 0; i < pending.actionCount; i++) {
    const action = actions[i] ?? actions[0];
    let r: BuildResult;
    if (i === index) {
      r = buildDecisions('edit', JSON.stringify({ edited_action: { name: action.name, args } }), one);
    } else if (pending.allowed.some((v) => canonicalVerb(v) === 'approve')) {
      r = buildDecisions('approve', '', one);
    } else {
      r = buildDecisions('edit', JSON.stringify({ edited_action: { name: action.name, args: action.args ?? {} } }), one);
    }
    if (!r.ok) return r;
    decisions.push(...r.decisions);
  }
  return { ok: true, decisions };
}

/** A custom verb the graph advertises: `{type: verb}` for every action. */
export function customDecisions(frame: Frame, verb: string): BuildResult {
  const { actionCount } = pendingFromFrame(frame);
  return { ok: true, decisions: Array.from({ length: actionCount }, () => ({ type: verb })) };
}

/** A one-line account of how an interrupt was answered, for the card once accepted. */
export function describeAnswer(decisions: Array<Record<string, unknown>>): string {
  const d = decisions.find((x) => canonicalVerb(String(x.type)) === 'edit') ?? decisions[0];
  if (!d) return 'Answered';
  const verb = String(d.type);
  const message = typeof d.message === 'string' && d.message ? d.message : undefined;
  switch (canonicalVerb(verb)) {
    case 'approve':
      return 'Approved';
    case 'reject':
      return message ? `Rejected: ${message}` : 'Rejected';
    case 'respond':
      return message ? `Responded: ${message}` : 'Responded';
    case 'edit':
      return 'Approved with edited arguments';
    default:
      return `Answered: ${verb}`;
  }
}
