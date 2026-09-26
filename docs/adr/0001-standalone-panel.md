# ADR 0001: A standalone LangStage panel over the existing stdio sidecar

- **Status:** Proposed (design phase; no feature code yet)
- **Date:** 2026-09-26
- **Deciders:** Kedar Dabhadkar (maintainer)
- **Related:** [OBJECTIVES.md](../../OBJECTIVES.md),
  [build plan](../plan-standalone-panel.md), langstage-core ADR 0001 (AG-UI wire),
  0006 (workspace as cwd), 0007 (family exit codes)

## Context

The extension's only UI today is the `@langstage` **chat participant**
(`vscode.chat.createChatParticipant`). The participant renders into Copilot's chat view,
so LangStage in the editor only works for someone who has:

1. **VS Code itself.** Cursor, Windsurf, VSCodium and code-server either lack the chat
   view or put their own assistant there.
2. **Copilot signed in.** Without a sign-in there is no chat view to host the
   participant.

The participant's rendering is also limited by the `ChatResponseStream` API:

- tool calls are a transient `progress` line followed by a ✓/❌ line;
- reasoning is italic markdown;
- todos are a markdown checklist that is re-emitted on every update;
- human-in-the-loop (HITL) answers work by resubmitting `@langstage /approve` as a new
  chat turn. The conversation id is smuggled through `ChatResult.metadata`, because VS
  Code exposes no conversation id (gh #133).

It also cannot be tested in CI: an automated run would need a Copilot sign-in. As a
result, no end-to-end or recorded test of the extension exists, only `hitl.ts` unit tests.

Claude Code's VS Code extension shows the other approach: the extension brings its own
webview UI and talks to a local agent process. Kedar has approved that direction for
LangStage, and the `@langstage` participant stays for Copilot users.

The pieces this decision builds on:

- **The sidecar** (`langstage_vscode/sidecar.py`) is a long-lived stdio process speaking
  NDJSON:
  - commands: `message`, `decision`, `cancel`, `shutdown`, each keyed by `session_id`,
    which becomes the LangGraph `thread_id`;
  - frames: `ready`, `ack`, `content` (with `message_id`), `reasoning`, `tool_start`,
    `tool_end`, `extraction` (todos), `interrupt` (with `allowed_decisions`), `complete`,
    `cancelled`, `error`, `turn_end`.

  It already has robust framing, per-session memory, cooperative cancel with rollback
  (gh #67/#106), validation of decision verbs through core's `normalize_decision`
  (gh #117), and startup-error reporting before `ready` (gh #131). It serves **one turn at
  a time**: a command for another session is queued behind the turn in flight.
- **The web app's frontend** (`langstage/frontend`, React 19 + Vite + Tailwind v4)
  consumes **the same frame vocabulary** (`content`, `tool_start`, `tool_end`,
  `extraction`, `interrupt`, `cancelled`, `complete`, `error`) over SSE plus REST POSTs,
  not AG-UI events.
  - Its chat components (`ChatPanel`, `MessageBubble`, `ToolCallCard`, `InterruptDialog`,
    `TodoPanel`) take their data as props.
  - One hook (`useAgentStream.ts`, 566 lines) mixes the transport, the frame reducer and
    `localStorage`.
  - There is no reasoning component and no session list.
- **Core's served AG-UI endpoint** (`build_app` / `serve` / `langstage-agui`) is FastAPI
  plus uvicorn and is already installed with the sidecar (`langstage-core[agui]` is a
  hard dependency).
  - Protocol: `POST /` takes a `RunAgentInput` and streams AG-UI SSE. Interrupts arrive as
    a `CUSTOM` `on_interrupt` event, and a resume is `RunAgentInput.resume[]` on the same
    `thread_id`.
  - It has **no auth or token**, **no cancel endpoint** (only a client disconnect), and
    **no todos/extraction event** on the served wire.
  - CORS is opt-in (`--cors`, where `loopback` means any localhost origin). It is off by
    default and has no environment variable.
- **Webview constraints:**
  - the page runs in an isolated `vscode-webview://` origin under a CSP we write;
  - local assets go through `asWebviewUri`;
  - the only channel to the extension host is `acquireVsCodeApi().postMessage`;
  - reaching `localhost` needs `portMapping` plus a CSP `connect-src`, and in remote or
    code-server setups the webview runs on a different machine from the extension host.

## Options

### A. Webview panel ⇄ extension host ⇄ existing stdio sidecar (postMessage ↔ NDJSON)

A `WebviewViewProvider` contributes a LangStage view to the activity bar. Its React bundle
posts typed messages to the extension host (`send`, `decide`, `cancel`, `newConversation`,
…). The host relays them as sidecar commands and forwards each frame back, tagged with its
conversation. The UI is a new, small React app. Its pieces are ported from the web
frontend's prop-driven components and re-themed with VS Code's `--vscode-*` CSS variables.

- **Security:** no network surface at all. The pipe belongs to the extension host, so no
  other local user or process can reach the agent. There are no tokens to mint or leak.
  The CSP can be `default-src 'none'` with nonce'd scripts and no `connect-src`.
- **Dependencies:** none new for users: the same `pip install langstage-vscode`. The VSIX
  gains one esbuild-bundled webview script (React + markdown), a few hundred KB.
- **Reuse:** the sidecar, its protocol, its tests, `hitl.ts` (`buildDecisions`,
  `summarizeActions`), and the web app's component designs and frame reducer. The frames
  are the same shape in both, so the ported code needs no event-mapping layer.
- **HITL:** a real inline approval card with Approve / Reject / Respond / Edit, the edit
  prefilled with the action's args. The answer is a `decision` on the conversation's
  `session_id`; the sidecar stays the authority on allowed verbs.
- **Sessions:** the host owns a `conversationId → session_id` map, now a first-class id
  rather than metadata smuggling. Transcripts are persisted host-side.
- **Cancel and streaming:** cooperative cancel and frame streaming already work.
- **Windows:** already proven; the participant uses this exact spawn and pipe.
- **Other editors:** Cursor, VSCodium and code-server work. It uses only stable API
  (`registerWebviewViewProvider`, `child_process`), and in a remote or code-server
  setup the webview-to-host channel is tunnelled by the editor itself.
- **Testing:**
  - the webview bundle is a plain web page, so Playwright can drive it in Chromium with a
    stub `acquireVsCodeApi` backed by the real sidecar running `--demo=tools`
    (keyless, deterministic, recordable);
  - code-server plus Playwright records the real editor;
  - `@vscode/test-electron` covers activation and the host relay.

  None of these needs Copilot.
- **Costs:**
  - A second UI codebase alongside the web and jupyter frontends: this is duplication
    until a shared chat package exists.
  - Turns are serialized per sidecar process, so a second conversation's message waits
    behind the first. The panel must show that the message is queued.
  - In-memory agent memory is lost when the window reloads. Stored transcripts will show
    more than the agent remembers unless the checkpointer is durable.

### B. Extension starts core's served AG-UI endpoint; webview uses an AG-UI client

The host runs `build_app` / `serve` on a random `127.0.0.1` port per workspace. The
webview connects through `portMapping` using `@ag-ui/client` (1.0: rxjs, zod, fast-json-patch).

- **For:**
  - reuses the family's *served* wire and the public AG-UI client, where CopilotKit-style
    UIs live;
  - any work on core's served path benefits the panel;
  - a panel built on AG-UI events could later be pointed at any AG-UI agent.
- **Against:**
  - **Security.** The endpoint has no authentication, so any process or user on the
    machine can drive an agent that has workspace write access. On shared hosts, which is
    exactly where code-server runs, this is a real exposure. Fixing it needs a bearer-token
    mechanism in core, a core release, and a token handed to the webview.
  - **Missing features.** There is no cancel endpoint: stopping a turn means dropping the
    connection, and the sidecar's rollback (gh #106) would have to be rebuilt in core.
    There is no todos/extraction event. Both need core changes.
  - **CORS and CSP.** The webview's origin (`vscode-webview://…`) is not in core's
    `loopback` CORS set, so core would need `*` or a new origin mode. The CSP also needs a
    per-launch `connect-src http://127.0.0.1:<port>`.
  - **Remote setups.** In remote, Codespaces and code-server setups, `portMapping` and
    port-forwarding behave differently from local ones. That is another failure class to
    test across the forks.
  - **Two transports.** The participant stays on stdio, so the extension would maintain
    both. The same HITL and cancel semantics would be implemented twice in TypeScript, and
    the mapping from AG-UI events (`TEXT_MESSAGE_*`, `TOOL_CALL_*`, `CUSTOM on_interrupt`)
    to the UI model is new code (about 1 to 2 days on its own).
  - **Process lifecycle.** A server process per workspace needs port selection, a
    readiness probe and orphan cleanup on Windows.
- **Extra cost over A:** about 1.5 to 2 weeks, including one or two core releases (auth,
  cancel, extraction events, CORS origin) and permanently more attack surface. The only
  new dependency is on the TypeScript side; the Python runtime is already installed.

### C. Embed the whole LangStage web app (`langstage run` on localhost) in the webview

- **For:** the cheapest to build (about 3 to 5 days, mostly an iframe, a port and
  lifecycle). It inherits every web feature and its Playwright suite.
- **Against:**
  - users must `pip install langstage`, the full web app;
  - the port is open and unauthenticated, as in B;
  - the iframe needs `portMapping` and a `frame-src` in the CSP;
  - the embedded UI is themed as a web page, not the editor, and duplicates the editor's
    own file explorer;
  - the web app has its own workspace, `cwd` and config handling alongside the
    extension's;
  - the extension's releases would be tied to the web app's.
  - It directly contradicts OBJECTIVES' anti-scope ("not the web app inside the editor").
    It would also make langstage-vscode a launcher for the web app rather than a stage
    of the family.
- **Cost:** cheap now, but expensive in support and security later. **Rejected.**

## Decision

**Option A.** Build a standalone webview panel that talks to the existing stdio sidecar
through the extension host, with a new lightweight React UI. The UI ports the web
frontend's prop-driven chat components and its frame reducer (same frame vocabulary),
re-themed for the editor.

Keep the `@langstage` Copilot participant, registered **only when `vscode.chat` exists**.
Refactor both surfaces onto one shared `SidecarClient` in the extension host.

This is the only option that:

- adds no network surface, no new user-side install and no core release;
- is testable and recordable in CI without Copilot or an API key;
- works the same in every VS Code-based editor.

It matches how Claude Code's extension works: a webview talking to a local process over
stdio.

AG-UI stays the family's wire *inside* the sidecar: core's in-process adapter drives
every turn (core ADR 0001). The panel consumes the sidecar's frames, as the web app
consumes `SessionAdapter`'s. If the family later moves browsers onto native AG-UI events,
the panel's reducer is the one place to change.

## Consequences

- **Scope:**
  - OBJECTIVES now names the panel as the primary surface and lists what it deliberately
    does not do (no autonomous edits, no implicit context, no ports, no web-app features).
  - The extension's `activationEvents` must include the view (`onView:langstage.chat`)
    and must not fail activation where `vscode.chat` is missing. Today `activate()` calls
    `vscode.chat.createChatParticipant` unconditionally.
- **Process model:**
  - The panel owns its own sidecar process. The participant keeps its own, including its
    restart-on-new-chat behavior, which would otherwise kill the panel's in-flight
    threads.
  - The two surfaces therefore do not share a conversation. That is acceptable, since a
    Copilot chat and the panel are different views.
- **No sidecar protocol change is required for v1.** One additive, optional change is
  planned: `ready` carrying `version` and `protocol` so the panel can warn about a
  sidecar that is too old. Clients already ignore unknown keys.
- **Duplication:** the port duplicates about 1,000 lines of web UI. The plan records the
  trigger for extracting a shared `@langstage/chat-ui` package (see Open questions)
  instead of doing it now.
- **Build:** the extension gains a bundler (esbuild) for the webview, a CSP with a nonce,
  and a `.vscodeignore` size budget.
- **Security:**
  - rendered agent output is untrusted: markdown only, no raw HTML, and links open
    through the host with `openExternal`;
  - the extension declares `capabilities.untrustedWorkspaces: { supported: false }`,
    because running the configured agent executes workspace code.
- **Distribution:** publish to **Open VSX** as well as the VS Code Marketplace; Cursor,
  VSCodium, Windsurf and code-server install from Open VSX. The extension is on neither
  marketplace today.
- **Testing:** a new webview Playwright harness, a code-server recording job and a
  test-electron smoke test join CI. This is the first end-to-end coverage the extension
  has had.

## Open questions

1. **Shared chat UI package.** When should the web, jupyter and vscode chat components be
   extracted into one published package?
   - Proposal: after the panel's M2 stabilizes, and once a second consumer (web or
     jupyter) agrees to adopt it.
   - The blockers are Tailwind v4 in web versus theme variables here, and npm publishing
     for the family.
2. **Placement.** The plan's default is an activity-bar view, which users can drag to the
   secondary sidebar. Should "Open in editor tab" (a `WebviewPanel`) be added as a
   follow-up?
3. **Concurrent conversations.** The sidecar runs one turn at a time. Queueing is fine for
   v1; true parallelism would need an async command loop in the sidecar.
4. **Memory across reloads.** Transcripts persist host-side, but the default in-memory
   checkpointer forgets them. The panel should label a restored conversation whose agent
   memory is gone (the planned banner). Should a later `history` command read the
   thread's messages from a durable checkpointer instead?
5. **Interpreter discovery.** Should the panel offer "Select Python interpreter"? It could
   use the Python extension's API when present; that extension is on Open VSX, but the
   forks' Python tooling varies. Separately, should it offer to `pip install
   langstage-vscode` when the sidecar is missing?
6. **React versus Preact.** React 19 keeps the port faithful to the web code; Preact
   would roughly halve the bundle. Measure in M1.
7. **Publisher accounts.** The VS Code Marketplace publisher `dkedar7` (an Azure DevOps
   personal access token) and the Open VSX `dkedar7` namespace claim are both still
   needed.
