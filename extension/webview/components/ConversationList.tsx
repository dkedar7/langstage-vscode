/**
 * The conversation switcher (build plan M4): the active conversation's title in the
 * header; opened, every conversation newest first with its turn state, and rename and
 * delete. Each conversation has its own session on the host (its own agent memory).
 * Delete removes the transcript only, and is offered only while no turn is in flight.
 */
import { useState } from 'react';
import type { PanelState } from '../state/reducer';
import { post } from '../vscodeApi';
import { Chevron, Spinner } from './icons';

export function ConversationList({ state }: { state: PanelState }) {
  const [open, setOpen] = useState(false);
  const [renaming, setRenaming] = useState<{ id: string; title: string } | undefined>();
  const [confirmDelete, setConfirmDelete] = useState<string | undefined>();
  const active = state.activeId ? state.conversations[state.activeId] : undefined;

  const commitRename = () => {
    if (renaming && renaming.title.trim()) {
      post({ type: 'conversation/rename', conversationId: renaming.id, title: renaming.title });
    }
    setRenaming(undefined);
  };

  return (
    <div className="ls-convs">
      <button
        type="button"
        className="ls-convs-toggle"
        aria-expanded={open}
        aria-label="Conversations"
        title="Conversations"
        onClick={() => setOpen(!open)}
      >
        <Chevron open={open} />
        <span className="ls-ellipsis">{active?.title ?? 'Conversations'}</span>
        {state.order.length > 1 && <span className="ls-convs-count">{state.order.length}</span>}
      </button>
      {open && (
        <ul className="ls-convs-list" aria-label="Conversation list">
          {state.order.map((id) => {
            const c = state.conversations[id];
            if (!c) return null;
            const isActive = id === state.activeId;
            if (renaming?.id === id) {
              return (
                <li key={id} className="ls-conv ls-conv-active">
                  <input
                    className="ls-conv-rename"
                    aria-label="Conversation title"
                    value={renaming.title}
                    autoFocus
                    onChange={(e) => setRenaming({ id, title: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename();
                      if (e.key === 'Escape') setRenaming(undefined);
                    }}
                    onBlur={commitRename}
                  />
                </li>
              );
            }
            return (
              <li key={id} className={`ls-conv${isActive ? ' ls-conv-active' : ''}`}>
                <button
                  type="button"
                  className="ls-conv-title"
                  aria-label={c.title}
                  aria-current={isActive ? 'true' : undefined}
                  onClick={() => {
                    post({ type: 'conversation/switch', conversationId: id });
                    setOpen(false);
                  }}
                >
                  {c.turn !== 'idle' && <Spinner />}
                  <span className="ls-ellipsis">{c.title}</span>
                  {c.turn === 'queued' && <span className="ls-muted">queued</span>}
                  {c.pending && <span className="ls-conv-flag">needs you</span>}
                </button>
                {confirmDelete === id ? (
                  <>
                    <button
                      type="button"
                      className="ls-btn ls-btn-small"
                      onClick={() => {
                        post({ type: 'conversation/delete', conversationId: id });
                        setConfirmDelete(undefined);
                      }}
                    >
                      Delete
                    </button>
                    <button type="button" className="ls-btn-secondary ls-btn-small" onClick={() => setConfirmDelete(undefined)}>
                      Keep
                    </button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="ls-icon-btn ls-conv-action"
                      title="Rename"
                      aria-label={`Rename ${c.title}`}
                      onClick={() => setRenaming({ id, title: c.title })}
                    >
                      ✎
                    </button>
                    <button
                      type="button"
                      className="ls-icon-btn ls-conv-action"
                      title={c.turn !== 'idle' ? 'Stop the turn before deleting' : 'Delete'}
                      aria-label={`Delete ${c.title}`}
                      disabled={c.turn !== 'idle'}
                      onClick={() => setConfirmDelete(id)}
                    >
                      ×
                    </button>
                  </>
                )}
              </li>
            );
          })}
          {!state.persistent && (
            <li className="ls-muted ls-convs-note">
              No folder is open, so conversations are kept only until the window closes.
            </li>
          )}
        </ul>
      )}
    </div>
  );
}
