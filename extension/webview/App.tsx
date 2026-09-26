import { useEffect, useReducer } from 'react';
import type { HostToWebview } from '../src/shared/panelProtocol';
import { Composer } from './components/Composer';
import { StatusLine } from './components/StatusLine';
import { TodoList } from './components/TodoList';
import { Transcript } from './components/Transcript';
import { initialState, pendingInterrupt, reduce } from './state/reducer';
import { post } from './vscodeApi';

function isHostMessage(data: unknown): data is HostToWebview {
  return !!data && typeof data === 'object' && (data as { v?: unknown }).v === 1 && typeof (data as { type?: unknown }).type === 'string';
}

export function App() {
  const [state, dispatch] = useReducer(reduce, initialState);

  useEffect(() => {
    const onMessage = (e: MessageEvent) => {
      if (isHostMessage(e.data)) dispatch(e.data);
    };
    window.addEventListener('message', onMessage);
    // The host answers with `restore`: it owns the transcript, so a rebuilt webview
    // comes back exactly as it was.
    post({ type: 'ui/ready' });
    return () => window.removeEventListener('message', onMessage);
  }, []);

  const conv = state.activeId ? state.conversations[state.activeId] : undefined;
  const paused = pendingInterrupt(conv);

  return (
    <div className="ls-app">
      <header className="ls-header">
        <StatusLine status={state.status} />
        <button
          type="button"
          className="ls-icon-btn"
          title="New conversation"
          aria-label="New conversation"
          onClick={() => post({ type: 'conversation/new' })}
        >
          +
        </button>
      </header>
      {conv ? (
        <>
          {conv.todos && conv.todos.length > 0 && <TodoList todos={conv.todos} />}
          <Transcript conv={conv} />
          <Composer
            turn={conv.turn}
            blockedReason={paused ? 'The agent is paused on the request above.' : undefined}
            onSend={(text) => post({ type: 'send', conversationId: conv.id, text })}
            onStop={() => post({ type: 'cancel', conversationId: conv.id })}
          />
        </>
      ) : (
        <div className="ls-empty ls-muted">Connecting…</div>
      )}
    </div>
  );
}
