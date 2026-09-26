/**
 * The message box: Enter sends, Shift+Enter is a newline; Stop (a cooperative `cancel`)
 * replaces Send while a turn is queued or running.
 */
import { useRef, useState } from 'react';
import type { TurnPhase } from '../state/reducer';

export function Composer({
  turn,
  blockedReason,
  onSend,
  onStop,
}: {
  turn: TurnPhase;
  blockedReason?: string;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState('');
  const ref = useRef<HTMLTextAreaElement>(null);
  const busy = turn !== 'idle';
  const canSend = !busy && !blockedReason && text.trim().length > 0;

  const send = () => {
    if (!canSend) return;
    onSend(text);
    setText('');
    ref.current?.focus();
  };

  return (
    <div className="ls-composer">
      {turn === 'queued' && <div className="ls-muted ls-composer-note">Queued behind another turn…</div>}
      {blockedReason && <div className="ls-muted ls-composer-note">{blockedReason}</div>}
      <textarea
        ref={ref}
        rows={3}
        value={text}
        placeholder="Message your agent (Enter to send, Shift+Enter for a newline)"
        aria-label="Message"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          }
        }}
      />
      <div className="ls-composer-actions">
        {busy ? (
          <button type="button" className="ls-btn-secondary" onClick={onStop}>
            Stop
          </button>
        ) : (
          <button type="button" className="ls-btn" disabled={!canSend} onClick={send}>
            Send
          </button>
        )}
      </div>
    </div>
  );
}
