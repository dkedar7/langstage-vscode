# Objectives & scope — langstage-vscode

*What this repo is for, who it serves, and what it deliberately is **not** — the yardstick
for deciding whether a proposed change or filed issue belongs here. When triaging an issue,
start here.*

## Objective

Run a langstage agent **inside VS Code and any VS Code-based editor** (VS Code with or
without Copilot, Cursor, VSCodium, Windsurf, code-server). The TypeScript extension renders;
the Python `langstage-vscode-sidecar` bridges an agent over stdio (`message` / `decision` /
`cancel`), with `--demo`, `--selfcheck`, `--repl`, `--message`, and `--show-config`.

The extension has two front ends over the **same sidecar and the same frame wire**
([ADR 0001](docs/adr/0001-standalone-panel.md)):

- the **LangStage panel** — the extension's own webview chat UI, which needs nothing but the
  editor and the sidecar. This is the primary surface.
- the **`@langstage` chat participant** — for Copilot users who want the agent inside the
  Copilot chat view. Kept, registered only where the editor provides the chat API.

## Who it's for

A user of a VS Code-based editor who wants to run *their own* LangGraph agent next to their
code — whether or not they have Copilot, and whichever fork they use.

## In scope

- The extension UX and the **sidecar protocol**: robust stdio framing, an `error` frame on bad
  input (never a crashed command loop), a workspace-faithful preflight (chdir to the workspace,
  like the real run path).
- Frame parity — the sidecar emits every frame type the extension is built to render
  (content / tool / reasoning / interrupt / extraction).
- **The LangStage panel** (ADR 0001): a webview chat that renders the frame wire — streamed
  text, reasoning, tool-call cards, the todos checklist, human-in-the-loop approval cards
  (every verb the interrupt allows), cancel — with per-conversation sessions (new / switch /
  delete) and a status line for the sidecar (starting, ready, failed + the startup error).
- Running in **any VS Code-based editor**: no hard dependency on proposed APIs, Copilot, or
  the `vscode.chat` API; published to both the VS Code Marketplace and **Open VSX**.
- Automated UI tests of the panel that need **no Copilot sign-in and no API key**
  (`--demo=tools`), including recorded runs in CI.

## Out of scope (anti-scope)

- The sidecar becoming a standalone CLI — that is **langstage-cli**.
- The extension becoming a full agent IDE. The panel is a *chat surface for the user's
  agent*, not a coding assistant. Precisely, the extension itself does **not**:
  - edit, create or delete workspace files, run terminals, or apply diffs on its own — any
    change to the workspace is done by the **agent's own tools**, inside the sidecar process,
    and is visible in the panel as a tool call (and gated by the agent's own HITL, if it has
    one);
  - read the editor's selection, open files, diagnostics or git state and inject them into
    the prompt (no implicit context gathering); the user types what the agent sees;
  - provide inline completions, code actions, CodeLens, a diff/"apply edit" review flow,
    or checkpoint/rewind of workspace files;
  - pick, host or proxy a model, hold API keys, or ship a default agent — the user brings the
    agent (`langstage.agentSpec`); `--demo` is a stub for trying the UI, not a product;
  - open a network port. The panel talks to the sidecar over the extension host's stdio pipe
    (ADR 0001); serving over HTTP is `langstage-agui` / the `langstage` web app.
- Re-implementing the web app inside the editor: no file browser, canvas, schedules, task
  board or token charts in the panel. Those belong to **langstage** (web).
- Duplicating langstage-core or langstage-cli logic in the sidecar — keep it a **thin bridge**.
  The panel adds UI, not agent behavior: HITL verbs are still validated by core's
  `normalize_decision` in the sidecar, not re-decided in TypeScript.

## How this fits the family

langstage-vscode is the **VS Code surface** of the family (for every VS Code-based editor). The sidecar is a thin bridge over
langstage-core; shared wire/behavior belongs in core, and terminal-runner concerns belong in
langstage-cli.

## Using this to triage

Before acting on an issue or PR: does it serve the objective above? Is it in scope or
anti-scope? Weigh its value — **security > correctness > advertised-≠-honored > DX/docs >
polish > net-new feature** — against the cost of a release. Then **fix, defer, or decline with
a reason.** Not every filed issue is worth acting on.
