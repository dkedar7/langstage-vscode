/**
 * One chat message. Ported from langstage/frontend `src/components/MessageBubble.tsx`
 * (langstage @ 0387426): user text is shown verbatim, assistant text as markdown;
 * re-themed with `--vscode-*` variables instead of Tailwind. Each assistant block is one
 * `message_id` (gh #108), so a planner's reply and the answer that follows are two
 * blocks rather than one glued paragraph.
 */
import { Markdown } from './Markdown';

export function UserMessage({ text }: { text: string }) {
  return (
    <div className="ls-msg ls-msg-user">
      <div className="ls-msg-label">You</div>
      <div className="ls-msg-body ls-pre">{text}</div>
    </div>
  );
}

export function AssistantMessage({
  text,
  streaming,
  showLabel,
}: {
  text: string;
  streaming: boolean;
  showLabel: boolean;
}) {
  return (
    <div className="ls-msg ls-msg-assistant">
      {showLabel && <div className="ls-msg-label">Agent</div>}
      <div className={`ls-msg-body${streaming ? ' ls-streaming' : ''}`}>
        <Markdown text={text} />
      </div>
    </div>
  );
}
