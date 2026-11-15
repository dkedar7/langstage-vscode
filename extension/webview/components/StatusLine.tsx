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

/** Which interpreter runs the agent and how it was chosen (gh #178), e.g. `.venv (auto-detected)`. */
function pythonLabel(s: PanelStatus): string | undefined {
  if (!s.python) return undefined;
  return s.python.how ?? s.python.path;
}

/** The tooltip: the agent, then the interpreter's full path. */
function readyTitle(s: PanelStatus): string {
  const how = s.python?.how ? ` (${s.python.how})` : '';
  return s.python ? `${agentLabel(s)}
Python: ${s.python.path}${how}` : agentLabel(s);
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
        <div className="ls-status" role="status" title={readyTitle(status)}>
          <span className="ls-dot ls-dot-ok" />
          <span className="ls-ellipsis">
            Ready · {agentLabel(status)}
            {pythonLabel(status) && <span className="ls-python"> · Python: {pythonLabel(status)}</span>}
          </span>
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
          {status.python && (
            <div className="ls-muted ls-python">
              Python: {status.python.path}
              {status.python.how ? ` (${status.python.how})` : ''}
            </div>
          )}
          {status.noAgent && (
            <div className="ls-muted">
              Set <code>langstage.agentSpec</code> (for example <code>./my_agent.py:graph</code>), or
              try the keyless demo agent.
            </div>
          )}
          <div className="ls-status-actions">
            {status.installCommand && (
              <button
                type="button"
                className="ls-btn"
                title={status.installCommand}
                onClick={() => post({ type: 'copy', text: status.installCommand ?? '' })}
              >
                Copy install command
              </button>
            )}
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
