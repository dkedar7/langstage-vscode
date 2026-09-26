// Small inline icons (no icon font: nothing is loaded outside the bundle).

export function Chevron({ open }: { open: boolean }) {
  return (
    <svg className={`ls-chevron${open ? ' ls-open' : ''}`} width="12" height="12" viewBox="0 0 16 16" aria-hidden>
      <path d="M6 4l4 4-4 4" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

export function Spinner() {
  return <span className="ls-spinner" role="status" aria-label="running" />;
}

export function StatusMark({ status }: { status: 'running' | 'success' | 'error' }) {
  if (status === 'running') return <Spinner />;
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
