/**
 * A tool call. Ported from langstage/frontend `src/components/ToolCallCard.tsx`
 * (langstage @ 0387426): a collapsible header with the name, a preview of the first
 * argument, the duration and a status mark; expanded, the arguments JSON, the result
 * (truncated, with an expand control), the error and any extraction. `tool_start` and
 * `tool_end` are paired by `id` in the reducer. Re-themed with `--vscode-*` variables;
 * the web card's canvas/iframe displays are not ported (no web-app features here).
 */
import { useState } from 'react';
import type { ToolItem } from '../state/reducer';
import { CopyButton } from './Markdown';
import { Chevron, StatusMark } from './icons';

const RESULT_PREVIEW = 2000;

function firstArgPreview(args: unknown): string | null {
  if (!args || typeof args !== 'object') return null;
  const values = Object.values(args as Record<string, unknown>);
  if (!values.length || values[0] == null) return null;
  const v = values[0];
  const s = typeof v === 'string' ? v : JSON.stringify(v);
  return s.length > 80 ? s.slice(0, 80) + '…' : s;
}

export function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null || ms < 0) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function ToolCard({ tool }: { tool: ToolItem }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState(false);
  const preview = firstArgPreview(tool.args);
  const duration = formatDuration(tool.durationMs);
  const hasArgs = !!tool.args && typeof tool.args === 'object' && Object.keys(tool.args).length > 0;
  const result = tool.result ?? '';
  const truncated = !full && result.length > RESULT_PREVIEW;

  return (
    <div className={`ls-tool ls-tool-${tool.status}`}>
      <button
        type="button"
        className="ls-tool-head"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <Chevron open={open} />
        <span className="ls-tool-name">{tool.name}</span>
        {preview && <code className="ls-tool-preview">{preview}</code>}
        <span className="ls-spacer" />
        {duration && <span className="ls-tool-duration">{duration}</span>}
        <StatusMark status={tool.status} />
      </button>
      {open && (
        <div className="ls-tool-body">
          {hasArgs && (
            <section>
              <div className="ls-section-label">Arguments</div>
              <pre className="ls-block">{JSON.stringify(tool.args, null, 2)}</pre>
            </section>
          )}
          {tool.result !== undefined && (
            <section>
              <div className="ls-section-label">
                Result <CopyButton text={result} />
              </div>
              <pre className="ls-block ls-scroll">
                {truncated ? result.slice(0, RESULT_PREVIEW) + '…' : result}
              </pre>
              {truncated && (
                <button type="button" className="ls-link" onClick={() => setFull(true)}>
                  Show all {result.length.toLocaleString()} characters
                </button>
              )}
            </section>
          )}
          {tool.errorMessage && (
            <section>
              <div className="ls-section-label ls-error-text">Error</div>
              <pre className="ls-block ls-error-block">{tool.errorMessage}</pre>
            </section>
          )}
          {tool.extraction && tool.extraction.type !== 'todos' && (
            <section>
              <div className="ls-section-label">Extracted · {tool.extraction.type}</div>
              <pre className="ls-block">{JSON.stringify(tool.extraction.data, null, 2)}</pre>
            </section>
          )}
          {tool.status === 'running' && <div className="ls-muted">Running…</div>}
        </div>
      )}
    </div>
  );
}
