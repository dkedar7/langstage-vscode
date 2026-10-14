// Small inline icons (no icon font: nothing is loaded outside the bundle).
import type { ToolStatus } from '../state/reducer';

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`ls-chevron${open ? ' ls-open' : ''}`} width="12" height="12" viewBox="0 0 16 16" aria-hidden>
      <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function Spinner() {
  return <span className="ls-spinner" role="img" aria-label="running" />;
}

export function StatusMark({ status }: { status: ToolStatus }) {
  if (status === 'running') return <Spinner />;
  if (status === 'cancelled') {
    return (
      <svg className="ls-stopped" width="14" height="14" viewBox="0 0 16 16" aria-label="did not finish">
        <circle cx="8" cy="8" r="5.5" fill="none" stroke="currentColor" strokeWidth="1.6" />
        <path d="M4.2 11.8l7.6-7.6" fill="none" stroke="currentColor" strokeWidth="1.6" />
      </svg>
    );
  }
  if (status === 'success') {
    return (
      <svg className="ls-ok" width="14" height="14" viewBox="0 0 16 16" aria-label="succeeded">
        <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" />
      </svg>
    );
  }
  return (
    <svg className="ls-bad" width="14" height="14" viewBox="0 0 16 16" aria-label="failed">
      <path d="M4 4l8 8M12 4l-8 8" fill="none" stroke="currentColor" strokeWidth="1.8" />
    </svg>
  );
}
