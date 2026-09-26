/** The conversation: messages, reasoning, tool cards, interrupts and errors, in order. */
import { useEffect, useRef } from 'react';
import type { ConversationView } from '../state/reducer';
import { ErrorBlock } from './ErrorBlock';
import { InterruptNotice } from './InterruptNotice';
import { AssistantMessage, UserMessage } from './Message';
import { ReasoningBlock } from './ReasoningBlock';
import { ToolCard } from './ToolCard';
import { Spinner } from './icons';

/** Label only the first reply block of a turn (tool cards and reasoning may precede it). */
function firstReplyOfTurn(conv: ConversationView, index: number): boolean {
  for (let j = index - 1; j >= 0; j--) {
    const kind = conv.items[j].kind;
    if (kind === 'user') return true;
    if (kind === 'assistant') return false;
  }
  return true;
}

export function Transcript({ conv }: { conv: ConversationView }) {
  const end = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  // Follow the stream while the reader is at the bottom; stop if they scroll up.
  useEffect(() => {
    if (stick.current) end.current?.scrollIntoView({ block: 'end' });
  });

  const last = conv.items[conv.items.length - 1];
  const waiting = conv.turn !== 'idle' && (!last || last.kind === 'user');

  return (
    <div
      className="ls-transcript"
      ref={box}
      onScroll={() => {
        const el = box.current;
        if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
    >
      {conv.items.length === 0 && (
        <div className="ls-empty">
          <div className="ls-empty-title">LangStage</div>
          <div className="ls-muted">
            Chat with your LangGraph agent. It runs in a local sidecar process, next to your code.
          </div>
        </div>
      )}
      {conv.items.map((item, i) => {
        switch (item.kind) {
          case 'user':
            return <UserMessage key={item.key} text={item.text} />;
          case 'assistant':
            return (
              <AssistantMessage
                key={item.key}
                text={item.text}
                streaming={item.streaming}
                showLabel={firstReplyOfTurn(conv, i)}
              />
            );
          case 'reasoning':
            return <ReasoningBlock key={item.key} text={item.text} streaming={item.streaming} />;
          case 'tool':
            return <ToolCard key={item.key} tool={item} />;
          case 'interrupt':
            return (
              <InterruptNotice
                key={item.key}
                frame={item.frame}
                pending={conv.turn === 'idle' && i === conv.items.length - 1}
              />
            );
          case 'error':
            return <ErrorBlock key={item.key} error={item.error} traceback={item.traceback} host={item.host} />;
          case 'notice':
            return (
              <div key={item.key} className="ls-notice">
                {item.text}
              </div>
            );
        }
      })}
      {waiting && (
        <div className="ls-notice">
          <Spinner /> {conv.turn === 'queued' ? 'Queued…' : 'Working…'}
        </div>
      )}
      <div ref={end} />
    </div>
  );
}
