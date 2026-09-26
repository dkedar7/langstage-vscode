/**
 * The conversation's live checklist. Ported from langstage/frontend
 * `src/components/TodoPanel.tsx` (langstage @ 0387426): progress header plus one row
 * per item. It is ONE list per conversation, updated in place from each `todos`
 * extraction (the chat participant re-emits a new markdown list on every update).
 */
import { useState } from 'react';
import type { TodoItem } from '../state/reducer';
import { Chevron } from './icons';

export function TodoList({ todos }: { todos: TodoItem[] }) {
  const [open, setOpen] = useState(true);
  const done = todos.filter((t) => t.status === 'completed').length;
  const pct = todos.length ? Math.round((done / todos.length) * 100) : 0;
  return (
    <div className="ls-todos">
      <button type="button" className="ls-todos-head" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Chevron open={open} />
        <span>Tasks</span>
        <span className="ls-spacer" />
        <span className="ls-muted">
          {done}/{todos.length}
        </span>
      </button>
      <div className="ls-progress" aria-hidden>
        <div style={{ width: `${pct}%` }} />
      </div>
      {open && (
        <ul className="ls-todo-list">
          {todos.map((t, i) => (
            <li key={i} className={`ls-todo ls-todo-${t.status}`}>
              <span className="ls-todo-mark" aria-label={t.status}>
                {t.status === 'completed' ? '✓' : t.status === 'in_progress' ? '◐' : '○'}
              </span>
              <span>{t.content}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
