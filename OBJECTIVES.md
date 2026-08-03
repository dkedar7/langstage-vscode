# Objectives & scope — langstage-vscode

*What this repo is for, who it serves, and what it deliberately is **not** — the yardstick
for deciding whether a proposed change or filed issue belongs here. When triaging an issue,
start here.*

## Objective

Run a langstage agent **inside VS Code**. The TypeScript extension renders; the Python
`langstage-vscode-sidecar` bridges an agent over stdio (`message` / `decision` / `cancel`),
with `--demo`, `--selfcheck`, `--repl`, `--message`, and `--show-config`.

## Who it's for

A VS Code user who wants an in-editor agent.

## In scope

- The extension UX and the **sidecar protocol**: robust stdio framing, an `error` frame on bad
  input (never a crashed command loop), a workspace-faithful preflight (chdir to the workspace,
  like the real run path).
- Frame parity — the sidecar emits every frame type the extension is built to render
  (content / tool / reasoning / interrupt / extraction).

## Out of scope (anti-scope)

- The sidecar becoming a standalone CLI — that is **langstage-cli**.
- The extension becoming a full agent IDE.
- Duplicating langstage-core or langstage-cli logic in the sidecar — keep it a **thin bridge**.

## How this fits the family

langstage-vscode is the **VS Code surface** of the family. The sidecar is a thin bridge over
langstage-core; shared wire/behavior belongs in core, and terminal-runner concerns belong in
langstage-cli.

## Using this to triage

Before acting on an issue or PR: does it serve the objective above? Is it in scope or
anti-scope? Weigh its value — **security > correctness > advertised-≠-honored > DX/docs >
polish > net-new feature** — against the cost of a release. Then **fix, defer, or decline with
a reason.** Not every filed issue is worth acting on.
