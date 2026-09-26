/** An `error` frame, inline in the transcript, with its `traceback` (if any) collapsed. */
import { useState } from 'react';
import { Chevron } from './icons';

export function ErrorBlock({ error, traceback, host }: { error: string; traceback?: string; host: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ls-error" role="alert">
      <div className="ls-error-title">{host ? 'The agent could not run' : 'Error'}</div>
      <div className="ls-pre">{error}</div>
      {traceback && (
        <>
          <button type="button" className="ls-link" aria-expanded={open} onClick={() => setOpen(!open)}>
            <Chevron open={open} /> Traceback
          </button>
          {open && <pre className="ls-block ls-scroll">{traceback}</pre>}
        </>
      )}
    </div>
  );
}
