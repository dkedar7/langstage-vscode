/**
 * Markdown for agent output. Agent output is untrusted (ADR 0001, Security):
 * - no `rehype-raw`, so raw HTML in the reply is never rendered as HTML;
 * - links are not followed by the webview: a click asks the host, which confirms and
 *   opens only http(s)/mailto through `openExternal`;
 * - remote images are blocked by the CSP (`img-src` is the extension and `data:` only).
 */
import { memo, useState, type ReactNode } from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { post } from '../vscodeApi';

function textOf(node: ReactNode): string {
  if (node === null || node === undefined || typeof node === 'boolean') return '';
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (typeof node === 'object' && 'props' in node) {
    return textOf((node as { props: { children?: ReactNode } }).props.children);
  }
  return '';
}

export function CopyButton({ text, label = 'Copy' }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="ls-copy"
      title={label}
      onClick={() => {
        post({ type: 'copy', text });
        setCopied(true);
        setTimeout(() => setCopied(false), 1200);
      }}
    >
      {copied ? 'Copied' : label}
    </button>
  );
}

const components: Components = {
  a({ href, children }) {
    return (
      <a
        href={href}
        title={href}
        onClick={(e) => {
          e.preventDefault();
          if (href) post({ type: 'openExternal', url: href });
        }}
      >
        {children}
      </a>
    );
  },
  pre({ children }) {
    return (
      <div className="ls-code">
        <CopyButton text={textOf(children).replace(/\n$/, '')} />
        <pre>{children}</pre>
      </div>
    );
  },
};

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="ls-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
