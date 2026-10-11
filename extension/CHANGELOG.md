# Changelog

Changes to the LangStage VS Code extension. The Python sidecar (`langstage-vscode` on
PyPI) is released separately; its changes, and this history in full, are in the
[repository CHANGELOG](https://github.com/dkedar7/langstage-vscode/blob/main/CHANGELOG.md).

## [0.6.3] - 2026-10-11

Needs no new sidecar.

### Fixed
- **The store page has a README** (gh #160). The extension package shipped no README or
  CHANGELOG, so its Marketplace, Open VSX and Extensions-view Details pages were blank,
  and nothing there said the extension needs the Python sidecar. The Details page now
  covers the two-part install (`pip install langstage-vscode`, then
  `langstage.pythonPath`), **Try the demo**, `langstage.agentSpec`, the settings and
  commands, with links to the docs.
- **A missing sidecar names the fix** (gh #160). When `langstage.pythonPath`'s interpreter
  doesn't have langstage-vscode, the panel used to show
  `sidecar exited before it was ready: ... No module named langstage_vscode`. It now says
  that the langstage-vscode package isn't installed in that interpreter, gives the
  `<python> -m pip install langstage-vscode` command for it, and adds a
  **Copy install command** button. `@langstage` shows the same message.

## [0.6.2] - 2026-09-30

Expects langstage-vscode 0.5.35 or newer, and warns about an older one.

### Added
- **The extension checks which sidecar it started** (gh #89), from the version, protocol
  and capabilities in the sidecar's `ready` frame:
  - A sidecar with a newer protocol than the extension supports is refused, with a message
    to update the extension.
  - A sidecar older than 0.5.35, or one that doesn't report its version, still runs, but a
    warning names the interpreter and its version and offers a **Copy command** button for
    `<python> -m pip install -U langstage-vscode`. It shows once per interpreter.
  - **Stop** sends `cancel` only to a sidecar that supports it. Otherwise it ends the turn
    in the extension and stops that sidecar process.

## [0.6.1] - 2026-09-30

### Changed
- **The "may not remember" note shows only when memory was lost** (gh #152). After a
  reload or a sidecar restart, the panel marks a restored conversation with *"The agent may
  not remember the conversation above"*. With a durable checkpointer (`SqliteSaver`,
  `PostgresSaver`, ...) the agent still has the conversation, so the note is no longer
  shown (needs sidecar 0.5.34 or later).

## [0.6.0] - 2026-09-26

The LangStage panel answers approvals, keeps several conversations and remembers them
across a window reload.

### Added
- **Approval cards.** A human-in-the-loop interrupt shows each requested action with its
  arguments, and one button per decision it allows: **Approve**, **Reject** with an
  optional reason, **Respond** with text, **Edit** with a JSON editor (checked before
  sending), and a button for any custom verb. If the sidecar refuses an answer, the card
  stays live and shows why. While a conversation is paused, the message box asks you to
  answer the request first.
- **Conversations.** The conversation list in the panel's header creates, switches,
  renames and deletes conversations. Each has its own session, so the agent's memory is
  never shared between them. A message sent while another conversation is streaming is
  queued and runs next.
- **Saved transcripts.** Conversations are saved per workspace, on this machine, and
  restored after a reload. When the agent process was replaced, the transcript says the
  agent may not remember it.

### Changed
- **New conversation** keeps the previous conversations.

## [0.5.0] - 2026-09-26

### Added
- **The LangStage panel (preview).** The extension's own chat view in the activity bar,
  which works with or without Copilot, in VS Code, Cursor, VSCodium, Windsurf and
  code-server
  ([ADR 0001](https://github.com/dkedar7/langstage-vscode/blob/main/docs/adr/0001-standalone-panel.md)).
  - Streamed markdown replies, tool-call cards, collapsed reasoning, and a live **Tasks**
    checklist.
  - **Stop** cancels the turn and keeps the conversation's memory.
  - A status line for the sidecar (starting, ready, failed with its startup error, or
    stopped), with **Open settings** and **Try the demo** when no agent is configured.
  - Links in agent output open only after you confirm them, and raw HTML is never
    rendered.
  - Commands: **Open the LangStage panel**, **New conversation**, **Restart the agent**.

### Changed
- The `@langstage` chat participant is registered only where the editor has a chat view,
  so the extension activates in Cursor, VSCodium and code-server.
- The extension is disabled in untrusted (Restricted Mode) workspaces, since running the
  agent executes workspace code.

## [0.4.0] - 2026-09-25

### Added
- **Answer an interrupt from the chat.** `@langstage` shows each requested action and a
  button per allowed decision (**Approve**, **Reject**, **Respond…**, **Edit…**), backed
  by the `/approve`, `/reject`, `/respond` and `/edit` commands.
- An installable `.vsix`, built by `npm run package` and by CI.

## [0.3.1] - 2026-07-23

### Fixed
- **Stop keeps the conversation's memory** (gh #69). Stop in the chat used to kill the
  sidecar, which erased the conversation's memory. It now cancels just the turn.

Earlier versions are in the
[repository CHANGELOG](https://github.com/dkedar7/langstage-vscode/blob/main/CHANGELOG.md).
