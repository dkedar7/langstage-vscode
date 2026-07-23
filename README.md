<p align="center">
  <img src="assets/header.svg" alt="langstage-vscode" width="100%">
</p>

# langstage-vscode

Chat with your own **LangGraph** agent from inside VS Code — in the
same chat panel as Copilot — via the `@langstage` chat participant.

> Renamed from **deepagent-vscode** (the old package name now just installs
> this one; `python -m deepagent_vscode` and the old sidecar command still work).

It has two parts in one repo:

- **`extension/`** — a TypeScript VS Code extension that registers the
  `@langstage` chat participant and renders agent output in the chat view.
- **`langstage_vscode/`** — a small Python **stdio sidecar** that loads your
  agent and streams its events. Built on
  [`langstage-core`](https://github.com/dkedar7/langstage-core),
  so it speaks the same typed event vocabulary as the other LangStage stages
  (`langstage`, `langstage-jupyter`, `langstage-cli`).

```
┌─ VS Code chat panel ────────────────────────────┐
│  @langstage  (TypeScript extension)              │
│        │  spawns                                 │
│        ▼                                          │
│  python -m langstage_vscode   (stdio sidecar)    │
│        │  NDJSON over stdin/stdout               │
│        ▼                                          │
│  your LangGraph / deepagents agent               │
└──────────────────────────────────────────────────┘
```

> **Status: early.** The extension is not yet on the VS Code Marketplace (run
> it from source for now), and interactive approval of human-in-the-loop
> interrupts is not wired into the chat UI yet (the sidecar already supports
> the round-trip, and [`--repl`](#configure) can drive it end to end from the
> CLI).

## Every stage for your LangGraph agent

langstage-vscode is the VS Code stage of the **LangStage family**: write your agent once — any LangGraph `CompiledGraph` — and run it on every stage with the same spec string (`module:attr` or `path/to/file.py:attr`), the same `langstage.toml` config file, and the same `LANGSTAGE_*` environment variables.

| Stage | Package | Try it |
|---|---|---|
| Web app | [langstage](https://github.com/dkedar7/langstage) | `langstage run --agent my_agent.py:graph` |
| JupyterLab | [langstage-jupyter](https://github.com/dkedar7/langstage-jupyter) | `pip install langstage-jupyter`, then the chat sidebar in `jupyter lab` |
| Terminal | [langstage-cli](https://github.com/dkedar7/langstage-cli) | `langstage-cli -a my_agent.py:graph` |
| VS Code | langstage-vscode | **you are here** |
| Reference agent | [langstage-hermes](https://github.com/dkedar7/langstage-hermes) | `LANGSTAGE_AGENT_SPEC=langstage_hermes.agent:graph` on any stage |
| Shared core | [langstage-core](https://github.com/dkedar7/langstage-core) | typed events + config resolver + AG-UI bridge behind every stage |

📖 **Full documentation:** <https://dkedar7.github.io/langstage-docs/>

### Serve over AG-UI

The sidecar already streams every turn through the in-process AG-UI adapter. Your
agent — any LangGraph `CompiledGraph` — can also be served over the
[AG-UI protocol](https://github.com/dkedar7/langstage-core) as a standalone HTTP
endpoint, without changing your agent code:

```bash
pip install "langstage-core[agui]"
langstage-agui --agent my_agent.py:graph
```

## Install

### Sidecar (Python)

```bash
pip install langstage-vscode
```

`--demo` (the keyless echo stub) runs on this base install — since 0.5.0 the base
deps pull the AG-UI runtime, which brings `langgraph`, so no extra is needed.

### Extension (from source, until it's on the Marketplace)

```bash
cd extension
npm install
npm run compile
```

Then press **F5** in VS Code (with the `extension/` folder open) to launch an
Extension Development Host with `@langstage` available.

## Configure

In VS Code settings:

| Setting | Description | Default |
|---|---|---|
| `langstage.agentSpec` | Your agent, as `path/to/agent.py:graph` or `module:graph` | _(falls back to `LANGSTAGE_AGENT_SPEC` / `langstage.toml`)_ |
| `langstage.pythonPath` | Python interpreter that has `langstage-vscode` installed | `python` |

The sidecar resolves its configuration through the family-standard chain —
**defaults < `langstage.toml` (global + project) < `LANGSTAGE_*` env < CLI
flags** — so a project with `[agent] spec = "my_agent.py:graph"` in its
`langstage.toml` needs no VS Code setting at all. Inspect the resolved values:

```bash
langstage-vscode-sidecar --show-config
```

Preflight the interpreter and your agent before wiring up chat — `--selfcheck`
(alias `--smoke`) loads the configured agent (or the demo stub), asserts it's a
runnable graph, drives one turn, and exits `0` (healthy) / non-zero with a precise
message (add `--json` for a machine-readable verdict):

```bash
langstage-vscode-sidecar --selfcheck                       # validate the runtime via the demo stub
langstage-vscode-sidecar --selfcheck --agent ./my.py:graph # validate the configured agent
```

`--selfcheck` answers "is the runtime healthy?"; **`--message`** answers "what does my
agent actually *say*?" — it drives one turn with your prompt and prints the reply, then
exits (no NDJSON + `shutdown` to hand-craft). Add `--json` to get the raw event frames
instead of the assembled text:

```bash
langstage-vscode-sidecar --demo --message "hello"                       # prints the reply
langstage-vscode-sidecar --agent ./my.py:graph --message "summarize the repo"
langstage-vscode-sidecar --agent ./my.py:graph --message "hi" --json    # raw event frames
```

`--message` answers "what does my agent say *once*?"; **`--repl`** answers "does it
*remember*?" — the multi-turn companion to `--message`. It reads one prompt per line and
drives a turn, but keeps **one long-lived session** (a single `session_id`, so a single
LangGraph `thread_id`) alive for every turn — the same per-conversation shape the VS Code
extension uses — so a **checkpointer-backed** agent's memory persists across turns. That
makes the checkpointer caveat below verifiable from the CLI in ten seconds: tell it your
name, ask on the next line. Exit with **Ctrl-D** (EOF) or a `:quit` line; `--json` streams
the raw event frames instead of the assembled text, just like `--message`:

```bash
langstage-vscode-sidecar --agent ./my.py:graph --repl
> my name is Kedar
...
> what is my name?
...
> :quit
```

An agent compiled with a checkpointer (`graph.compile(checkpointer=MemorySaver())`) will
recall the first line on the second; one without a checkpointer won't — which is exactly the
missing-checkpointer / wrong-`session_id` mistake to catch before wiring up the extension
(see the memory note under [Sidecar protocol](#sidecar-protocol)).

Both turn-drivers are **interrupt-aware**. If your agent pauses on a human-in-the-loop
`interrupt(...)` (the common `deepagents` / LangGraph approval pattern), the turn is no longer
a silent blank — the pending action and the decisions it allows are surfaced on **stderr**
(stdout stays the clean reply channel), and one-shot `--message` exits with a distinct code
**`2`** so an interrupt is scriptable, distinct from a clean reply (`0`) or an error (`1`):

```console
$ langstage-vscode-sidecar --agent ./hitl.py:graph --message "do it"
interrupt: agent paused awaiting a decision
  action: confirm   allowed: reject | edit | respond | approve
  resume by sending a `decision` command (add --json to see the full request)
$ echo $?
2
```

With `--json`, the raw `{"type": "interrupt", ...}` frame streams on stdout, so a consumer keys
on it directly.

**`--repl` can also *answer* the interrupt**, completing the `interrupt` → `decision` round-trip
without hand-writing the stdio protocol. When a turn ends on an interrupt, the session enters
**decision mode**: the next line becomes a `decision` on the *same* session, so it resumes that
thread's pending interrupt.

```console
$ printf 'do it\napprove\n:quit\n' | langstage-vscode-sidecar --agent ./hitl.py:graph --repl
interrupt: agent paused awaiting a decision
  action: confirm   allowed: reject | edit | respond | approve
  answer it here: `:decision <verb>` (or a bare `<verb>`) using a verb above
  payloads: reject [<text>] | edit <json> | respond <text>
resumed with: {'decisions': [{'type': 'approve'}]}
```

- Type **`:decision <verb>`** (same `:`-prefixed namespace as `:quit`), or just the **bare verb** —
  a bare verb is only read as a decision *while an interrupt is pending*; the rest of the time
  `approve` is ordinary chat text.
- The verbs come from **that interrupt's own `allowed_decisions`**, so an approval-only agent
  offers and accepts exactly `reject | approve`. Payloads follow the LangChain HITL decisions:
  `approve`, `reject [<text>]`, `respond <text>`, `edit <json>` (free text becomes `message`, a
  JSON object is merged in, e.g. `edit {"edited_action": {"name": "confirm", "args": {}}}`).
- While an interrupt is pending, a line that **isn't** a valid decision is **refused on stderr and
  re-prompted** with the interrupt left pending — it is never silently sent as a new message (which
  would just re-interrupt and look accepted) and never swallowed. `:quit` is always the way out.
- `--json` composes: the answer line emits `ack` with `"ref": "decision"`, so the trace reads
  `ready → ack message → interrupt → complete → turn_end → ack decision → content → complete → turn_end`.
  (Both turns emit `complete` — an interrupt turn is `interrupt → complete → turn_end`; it is
  paused, not finished-with-a-reply, so detect the pause via the `interrupt` frame, not the
  absence of `complete`.)
- **`--repl` exit codes:** `0` on a clean session (including an interrupt that *was* answered),
  `1` if the agent could not start at all, and **`2`** if the session ends with an interrupt still
  unanswered — the same "paused awaiting a decision" signal `--message` uses.

You can still drive `decision` over the raw stdio protocol directly
(`{"type": "decision", "session_id": "...", "decisions": [{"type": "approve"}]}`) — that is what
the VS Code extension does, since interactive approval is not wired into the chat UI yet.

Your agent is any LangGraph `CompiledGraph` (e.g. from `deepagents`), exported
under the name in the spec:

```python
# my_agent.py
from deepagents import create_deep_agent
graph = create_deep_agent(...)   # -> langstage.agentSpec = "my_agent.py:graph"
```

## Usage

Open the chat panel and start a message with `@langstage`:

```
@langstage summarize the failing tests in this repo and propose a fix
```

The extension streams the agent's content, tool calls, reasoning, and todo
updates into the chat response.

## Sidecar protocol

The extension talks to the sidecar over newline-delimited JSON. You can drive it
directly for testing:

```bash
LANGSTAGE_AGENT_SPEC=./my_agent.py:graph python -m langstage_vscode

# or with no agent and no API key at all — the keyless stub runs on a base install
python -m langstage_vscode --demo
```

**Commands** (client → sidecar), one JSON object per line:

```jsonc
{"type": "message",  "session_id": "s1", "content": "hello"}
{"type": "decision", "session_id": "s1", "decisions": [{"type": "approve"}]}
{"type": "cancel",   "session_id": "s1"}   // abort the in-flight turn, keep the session
{"type": "shutdown"}
```

A **`cancel`** stops the turn currently streaming for that `session_id` **cooperatively** —
it emits a distinct `cancelled` frame (neither `complete` nor `error`) then `turn_end`, and
**leaves the process, the session, and its in-process checkpointer alive**, so the next
`message` on the same `session_id` resumes with memory intact. That is the difference from
killing the sidecar to stop a turn, which throws the conversation's memory away. A `cancel`
with no turn in flight for the session is answered with an `error` frame
(`no turn in progress for session '…'`), consistent with the `decision`/`message` guards.

**Events** (sidecar → client) — the `event_to_dict()` shapes from
`langstage-core`, plus a few protocol frames:

```jsonc
{"type": "ready"}                          // emitted once at startup
{"type": "ack", "ref": "message"}          // command accepted
{"type": "content", "content": "..."}      // assistant text
{"type": "tool_start", "name": "...", ...} // tool call
{"type": "tool_end", "name": "...", ...}   // tool result
{"type": "interrupt", "action_requests": [...]}  // human-in-the-loop
{"type": "complete"}                       // turn finished (success) — see the note below
{"type": "cancelled", "session_id": "s1"}  // turn stopped by a `cancel` (not complete/error)
{"type": "error", "error": "..."}          // protocol error (bad/unknown command)
                                           // OR an exception raised by the agent.
                                           // On agent failure the turn emits this
                                           // INSTEAD of "complete", then "turn_end".
{"type": "turn_end", "session_id": "s1"}
```

> A client must handle `error`: a malformed/unknown command, a `message` with no
> `content`, an invalid `decision` (including a well-formed one sent when the session
> has no pending interrupt to resume), **and** an agent crashing mid-turn all emit an
> `error` frame. On the agent-failure path there is no `complete` — the sequence
> is `ack → error → turn_end` — so don't key turn-completion off `complete` alone.
>
> Two more terminal shapes are *not* `complete`. An **interrupt** turn emits
> `interrupt → complete → turn_end`: it *does* still emit `complete`, but the agent
> produced no reply — it is paused awaiting a decision, so detect the pause via the
> `interrupt` frame, not by the presence of `complete`. A **cancelled** turn (a client
> `cancel`) emits `cancelled → turn_end` with **no** `complete` at all — a cancelled turn
> is neither `complete` nor `error`.

> **`session_id` and conversational memory.** The sidecar maps each
> `session_id` to a LangGraph `thread_id` in the run config. Multi-turn memory
> across messages with the same `session_id` therefore only works if your agent
> was **compiled with a checkpointer** (e.g. `graph.compile(checkpointer=...)`,
> or `create_deep_agent(..., checkpointer=...)`). A plain `create_react_agent`
> graph with no checkpointer is stateless: the second turn won't remember the
> first, even with a matching `session_id`. This is expected LangGraph
> behavior, not a sidecar bug.
>
> The **VS Code extension keeps one sidecar process alive per conversation** —
> it spawns the sidecar on the first `@langstage` message and reuses that same
> process (and the same `session_id`) for every following turn — so an
> **in-process** checkpointer like `MemorySaver` persists across turns in chat,
> not just when you drive the stdio protocol by hand (gh #54). The process is
> restarted on a config change (interpreter / agent spec) and when you start a
> new chat, so a new conversation begins with a clean thread. If you drive the
> sidecar yourself, keep **one process** alive and send each turn to it — a fresh
> process per message gets a fresh in-process checkpointer and forgets the prior
> turn; a **persistent** checkpointer (`SqliteSaver`, `PostgresSaver`, …) keyed
> by `thread_id` is what survives across separate processes. The **`--repl`** flag
> (see [Configure](#configure)) does exactly this — one process, one session across
> turns — so you can verify this memory behavior from the CLI without hand-crafting
> the protocol.

## Development

```bash
# Sidecar
pip install -e ".[dev]"
pytest

# Extension
cd extension
npm install
npm run compile
```

## License

MIT
