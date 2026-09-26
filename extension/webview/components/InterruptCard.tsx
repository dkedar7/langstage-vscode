/**
 * The approval card (build plan M3): what the agent wants to do, and one button per verb
 * in the interrupt's `allowed_decisions`.
 *
 * Ported from langstage/frontend `src/components/InterruptDialog.tsx` (langstage @
 * 681c81e): the action list with its arguments, Approve / Reject, and Edit with a JSON
 * editor prefilled with the action's arguments. Changed for the panel: it is an inline
 * card in the transcript rather than a modal, Reject takes an optional reason, Respond
 * answers in words, a custom verb gets a generic button, and a decision the sidecar
 * refuses (`error → turn_end`, no `ack`) keeps the card live with the error shown.
 * Re-themed with `--vscode-*` variables.
 */
import { useState } from 'react';
import { summarizeActions } from '../../src/hitl';
import type { BuildResult } from '../../src/hitl';
import {
  cardVerbs,
  customDecisions,
  describeAnswer,
  editDecisions,
  simpleDecisions,
} from '../state/decisions';
import type { InterruptItem, Pending } from '../state/reducer';
import { Spinner, StatusMark } from './icons';

type Form = { kind: 'reject' | 'respond'; text: string } | { kind: 'edit'; index: number; text: string };

export function InterruptCard({
  item,
  pending,
  busy,
  onDecide,
}: {
  item: InterruptItem;
  /** Set when this card is the conversation's pending interrupt. */
  pending: Pending | undefined;
  /** A turn is queued or running in this conversation. */
  busy: boolean;
  onDecide: (decisions: Array<Record<string, unknown>>) => void;
}) {
  const [form, setForm] = useState<Form | undefined>();
  const [invalid, setInvalid] = useState<string | undefined>();
  const frame = item.frame;
  const actions = summarizeActions(frame);
  const verbs = cardVerbs(frame);
  const live = !!pending;
  const sending = live && (!!pending.sent || busy);
  const canEdit = live && verbs.some((v) => v.canon === 'edit');

  const submit = (r: BuildResult) => {
    if (!r.ok) {
      setInvalid(r.reason.replace(/`\/(\w+)`/g, '$1'));
      return;
    }
    setInvalid(undefined);
    setForm(undefined);
    onDecide(r.decisions);
  };

  const openEdit = (index: number) =>
    setForm({ kind: 'edit', index, text: JSON.stringify(actions[index]?.args ?? {}, null, 2) });

  return (
    <div
      className={`ls-interrupt${live ? ' ls-interrupt-live' : ''}`}
      role="group"
      aria-label="Approval request"
    >
      <div className="ls-interrupt-title">
        {live ? 'The agent is waiting for your decision' : 'The agent asked for a decision'}
      </div>
      {actions.map((a, i) => (
        <div key={i} className="ls-interrupt-action">
          <div className="ls-interrupt-action-head">
            <code className="ls-tool-name">{a.name}</code>
            {canEdit && actions.length > 1 && (
              <button type="button" className="ls-link" disabled={sending} onClick={() => openEdit(i)}>
                Edit arguments
              </button>
            )}
          </div>
          {a.description && <div className="ls-pre">{a.description}</div>}
          {a.args !== undefined && <pre className="ls-block">{JSON.stringify(a.args, null, 2)}</pre>}
        </div>
      ))}

      {live && form && (
        <div className="ls-interrupt-form">
          <label className="ls-section-label" htmlFor={`ls-form-${item.key}`}>
            {form.kind === 'edit'
              ? `Arguments for ${actions[form.index]?.name ?? 'the action'} (JSON)`
              : form.kind === 'reject'
                ? 'Reason (optional)'
                : 'Your response'}
          </label>
          <textarea
            id={`ls-form-${item.key}`}
            className={form.kind === 'edit' ? 'ls-json' : undefined}
            rows={form.kind === 'edit' ? 6 : 2}
            value={form.text}
            autoFocus
            spellCheck={false}
            onChange={(e) => {
              setInvalid(undefined);
              setForm({ ...form, text: e.target.value });
            }}
          />
          <div className="ls-interrupt-buttons">
            <button
              type="button"
              className="ls-btn"
              disabled={sending || (form.kind === 'respond' && !form.text.trim())}
              onClick={() =>
                submit(
                  form.kind === 'edit'
                    ? editDecisions(frame, form.index, form.text)
                    : simpleDecisions(frame, form.kind, form.text),
                )
              }
            >
              {form.kind === 'reject' ? 'Reject' : form.kind === 'respond' ? 'Send response' : 'Approve with edits'}
            </button>
            <button type="button" className="ls-btn-secondary" onClick={() => (setForm(undefined), setInvalid(undefined))}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {live && !form && (
        <div className="ls-interrupt-buttons">
          {verbs.map(({ verb, canon }) => {
            switch (canon) {
              case 'approve':
                return (
                  <button key={verb} type="button" className="ls-btn" disabled={sending}
                    onClick={() => submit(simpleDecisions(frame, 'approve'))}>
                    Approve
                  </button>
                );
              case 'edit':
                return (
                  <button key={verb} type="button" className="ls-btn-secondary" disabled={sending}
                    onClick={() => openEdit(0)}>
                    Edit…
                  </button>
                );
              case 'respond':
                return (
                  <button key={verb} type="button" className="ls-btn-secondary" disabled={sending}
                    onClick={() => setForm({ kind: 'respond', text: '' })}>
                    Respond…
                  </button>
                );
              case 'reject':
                return (
                  <button key={verb} type="button" className="ls-btn-secondary" disabled={sending}
                    onClick={() => setForm({ kind: 'reject', text: '' })}>
                    Reject…
                  </button>
                );
              default:
                return (
                  <button key={verb} type="button" className="ls-btn-secondary" disabled={sending}
                    onClick={() => submit(customDecisions(frame, verb))}>
                    {verb}
                  </button>
                );
            }
          })}
          {verbs.length === 0 && (
            <span className="ls-muted">The request lists no allowed decisions, so it can't be answered here.</span>
          )}
        </div>
      )}

      {live && sending && (
        <div className="ls-notice">
          <Spinner /> Sending your decision…
        </div>
      )}
      {invalid && <div className="ls-error-text" role="alert">{invalid}</div>}
      {live && pending.error && !sending && (
        <div className="ls-interrupt-refused" role="alert">
          <strong>The agent refused this answer.</strong> The request is still pending.
          <div className="ls-pre ls-status-error">{pending.error}</div>
        </div>
      )}
      {item.answer && (
        <div className="ls-interrupt-answer">
          <StatusMark status="success" /> {describeAnswer(item.answer)}
        </div>
      )}
      {item.expired && (
        <div className="ls-muted">
          No longer pending: the agent restarted, so this request can't be answered. Send a new
          message to carry on.
        </div>
      )}
    </div>
  );
}
