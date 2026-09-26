/**
 * An `interrupt` frame: what the agent wants to do and the verbs it allows. Read-only in
 * this preview; the approval card with Approve / Reject / Respond / Edit is milestone M3
 * (a port of the web app's InterruptDialog). Uses hitl.ts `summarizeActions`, the same
 * reading of the three interrupt shapes the chat participant uses.
 */
import type { Frame } from '../../src/shared/panelProtocol';
import { summarizeActions } from '../../src/hitl';

export function InterruptNotice({ frame, pending }: { frame: Frame; pending: boolean }) {
  const actions = summarizeActions(frame);
  const allowed = Array.isArray(frame.allowed_decisions) ? frame.allowed_decisions.map(String) : [];
  return (
    <div className="ls-interrupt">
      <div className="ls-interrupt-title">The agent is waiting for your decision</div>
      {actions.map((a, i) => (
        <div key={i} className="ls-interrupt-action">
          <strong>{a.name}</strong>
          {a.description && <span>: {a.description}</span>}
          {a.args !== undefined && <pre className="ls-block">{JSON.stringify(a.args, null, 2)}</pre>}
        </div>
      ))}
      {allowed.length > 0 && <div className="ls-muted">Allowed: {allowed.join(' · ')}</div>}
      {pending && (
        <div className="ls-muted">
          Answering from the panel arrives in the next preview. Until then, start a new
          conversation to continue.
        </div>
      )}
    </div>
  );
}
