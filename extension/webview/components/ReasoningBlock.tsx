/**
 * Model reasoning (`reasoning` frames), kept apart from the reply. New: the web app has
 * no equivalent. Collapsed by default; while it streams, the header says so and the
 * latest line is previewed, so it is visible without taking over the transcript.
 */
import { useState } from 'react';
import { Chevron, Spinner } from './icons';

export function ReasoningBlock({ text, streaming }: { text: string; streaming: boolean }) {
  const [open, setOpen] = useState(false);
  const tail = text.trim().split(/\n/).pop() ?? '';
  return (
    <div className="ls-reasoning">
      <button type="button" className="ls-reasoning-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Chevron open={open} />
        <span>{streaming ? 'Thinking…' : 'Reasoning'}</span>
        {streaming && <Spinner />}
        {!open && <span className="ls-reasoning-preview">{tail}</span>}
      </button>
      {open && <div className="ls-reasoning-body ls-pre">{text}</div>}
    </div>
  );
}
