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
> the round-trip).

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
{"type": "shutdown"}
```

**Events** (sidecar → client) — the `event_to_dict()` shapes from
`langstage-core`, plus a few protocol frames:

```jsonc
{"type": "ready"}                          // emitted once at startup
{"type": "ack", "ref": "message"}          // command accepted
{"type": "content", "content": "..."}      // assistant text
{"type": "tool_start", "name": "...", ...} // tool call
{"type": "tool_end", "name": "...", ...}   // tool result
{"type": "interrupt", "action_requests": [...]}  // human-in-the-loop
{"type": "complete"}                       // turn finished (success)
{"type": "error", "error": "..."}          // protocol error (bad/unknown command)
                                           // OR an exception raised by the agent.
                                           // On agent failure the turn emits this
                                           // INSTEAD of "complete", then "turn_end".
{"type": "turn_end", "session_id": "s1"}
```

> A client must handle `error`: a malformed/unknown command, a `message` with no
> `content`, an invalid `decision`, **and** an agent crashing mid-turn all emit an
> `error` frame. On the agent-failure path there is no `complete` — the sequence
> is `ack → error → turn_end` — so don't key turn-completion off `complete` alone.

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
