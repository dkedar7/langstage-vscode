/**
 * The sidecar's status: starting, ready, failed (with the startup error verbatim,
 * gh #131) or stopped. With no agent configured it offers Open settings and Try the
 * demo (the keyless `--demo=tools` agent, for this session only).
 */
import { useState } from 'react';
import type { PanelStatus } from '../../src/shared/panelProtocol';
import { post } from '../vscodeApi';
import { Chevron, Spinner } from './icons';

function agentLabel(s: PanelStatus): string {
  if (s.demo) return 'demo agent (--demo=tools)';
  return s.agentSpec || 'agent from langstage.toml / LANGSTAGE_AGENT_SPEC';
}

export function StatusLine({ status }: { status: PanelStatus }) {
  const [showLog, setShowLog] = useState(false);
  switch (status.phase) {
    case 'idle':
    case 'starting':
      return (
        <div className="ls-status" role="status">
          <Spinner /> <span>Starting the agent…</span>
        </div>
      );
    case 'ready':
      return (
        <div className="ls-status" role="status" title={agentLabel(status)}>
          <span className="ls-dot ls-dot-ok" />
          <span className="ls-ellipsis">Ready · {agentLabel(status)}</span>
        </div>
      );
    case 'stopped':
      return (
        <div className="ls-status ls-status-warn" role="status">
          <span className="ls-dot ls-dot-warn" />
          <span>The agent stopped.</span>
          <span className="ls-spacer" />
          <button type="button" className="ls-btn-secondary" onClick={() => post({ type: 'restartSidecar' })}>
            Restart
          </button>
        </div>
      );
    case 'failed':
      return (
        <div className="ls-status ls-status-failed" role="alert">
          <div className="ls-status-row">
            <span className="ls-dot ls-dot-bad" />
            <strong>{status.noAgent ? 'No agent configured' : 'The agent failed to start'}</strong>
          </div>
          <div className="ls-pre ls-status-error">{status.error}</div>
          {status.noAgent && (
            <div className="ls-muted">
              Set <code>langstage.agentSpec</code> (for example <code>./my_agent.py:graph</code>), or
              try the keyless demo agent.
            </div>
          )}
          <div className="ls-status-actions">
            <button type="button" className="ls-btn" onClick={() => post({ type: 'openSettings' })}>
              Open settings
            </button>
            {status.noAgent && (
              <button type="button" className="ls-btn-secondary" onClick={() => post({ type: 'tryDemo' })}>
                Try the demo
              </button>
            )}
            <button type="button" className="ls-btn-secondary" onClick={() => post({ type: 'restartSidecar' })}>
              Retry
            </button>
          </div>
          {status.stderrTail && (
            <>
              <button type="button" className="ls-link" onClick={() => setShowLog(!showLog)}>
                <Chevron open={showLog} /> stderr
              </button>
              {showLog && <pre className="ls-block ls-scroll">{status.stderrTail}</pre>}
            </>
          )}
        </div>
      );
  }
}
