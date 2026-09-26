import { useEffect, useReducer } from 'react';
import type { HostToWebview } from '../src/shared/panelProtocol';
import { Composer } from './components/Composer';
import { ConversationList } from './components/ConversationList';
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
        <ConversationList state={state} />
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
      <div className="ls-statusbar">
        <StatusLine status={state.status} />
      </div>
      {conv ? (
        <>
          {conv.todos && conv.todos.length > 0 && <TodoList todos={conv.todos} />}
          <Transcript
            conv={conv}
            onDecide={(decisions) => post({ type: 'decide', conversationId: conv.id, decisions })}
          />
          <Composer
            turn={conv.turn}
            // gh #134: while paused, a new message would not reach the agent (the sidecar
            // refuses it too); the panel says so instead of sending it.
            blockedReason={paused ? 'Answer the request above to continue.' : undefined}
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
