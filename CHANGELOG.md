# Changelog

All notable changes to this project will be documented in this file.

## [0.4.2] - 2026-06-20

### Fixed
- **Custom agents that return a finished `AIMessage` rendered an empty chat turn
  (gh #-dogfood).** The sidecar runs dual `stream_mode=["updates","messages"]`,
  where content used to come only from token streaming — so a `CompiledGraph`
  whose node returns a prebuilt `AIMessage` (rule-based / router / retrieval, or
  any non-token-streaming LLM call) produced no content frame. Modernized the
  `langgraph-stream-parser` pin from the stale `<0.5` to `>=0.6.4,<0.7`, which
  emits such content as a fallback. Verified end to end over NDJSON.

### Docs
- The protocol-section keyless `--demo` example now notes it needs the `[demo]`
  extra (`pip install "langstage-vscode[demo]"`); a base install ships only the
  sidecar, and `--demo`'s stub agent needs langgraph.

## [0.4.1] - 2026-06-19

### Fixed
- `langstage_vscode.__version__` was a hard-coded `"0.1.0"` and had drifted (the
  package was at 0.4.0). Since it's an exported public attribute (`__all__`), any
  consumer trusting it got the wrong answer. It now derives from the installed
  distribution metadata (`importlib.metadata.version`), so it always matches
  `pyproject.toml` and can't drift again. (gh #9)

### Docs
- Document that `session_id` ↔ `thread_id` multi-turn memory only works when the
  agent was compiled with a checkpointer; a plain `create_react_agent` is
  stateless across turns. (gh #9, adopter observation)

## [0.4.0] - 2026-06-14

Adopt AG-UI: widen the langgraph-stream-parser ceiling to <0.5 and add an [agui] extra so this surface's agent can be served over AG-UI via langstage-agui. Additive; no runtime changes.

## [0.3.0] - 2026-06-12

**deepagent-vscode is now `langstage-vscode`** — the VS Code stage of the LangStage family ("every stage for your LangGraph agent").

### Changed

- Distribution `deepagent-vscode` → **`langstage-vscode`**; module `deepagent_vscode` → **`langstage_vscode`**. A deprecated alias package keeps `import deepagent_vscode` and `python -m deepagent_vscode` working (with a `DeprecationWarning`); the `deepagent-vscode-sidecar` command remains as an alias of `langstage-vscode-sidecar`.
- Extension: chat participant is **`@langstage`** (`langstage.agent`); settings move to `langstage.agentSpec` / `langstage.pythonPath` — the old `deepagent.*` settings are still read as deprecated fallbacks.
- Canonical config vocabulary via langgraph-stream-parser 0.3: `LANGSTAGE_*` env vars, project `langstage.toml`, global `~/.langstage/config.toml`; full legacy vocabulary still resolves.
- Parser pinned `>=0.3,<0.4`.


## [0.2.0] - 2026-06-10

First PyPI release of the sidecar (`pip install deepagent-vscode`).

### Added

- **Family-standard config chain** — the sidecar resolves through the shared `HostConfig`: defaults < `deepagents.toml` (global + project) < `DEEPAGENT_*` env < CLI flags. A project `deepagents.toml` with `[agent] spec = "..."` now just works.
- **`--demo`** — run with the shared keyless echo agent (`langgraph_stream_parser.demo.stub:graph`); no API key needed.
- **`--show-config`** — print each resolved value with its source and the env var / TOML key that sets it.
- **Extension**: an empty `deepagent.agentSpec` setting no longer hard-errors — the extension spawns the sidecar without `--agent` (cwd anchored at the workspace) and lets the config chain resolve.
- README: *One agent, every surface* family table.

### Changed

- `langgraph-stream-parser` pinned `>=0.2.2,<0.3`.

## [0.1.0] - 2026-06-04

Initial version (GitHub only): stdio sidecar bridging LangGraph agents to the `@deepagent` VS Code chat participant, speaking the `langgraph-stream-parser` `event.to_dict()` wire vocabulary.
