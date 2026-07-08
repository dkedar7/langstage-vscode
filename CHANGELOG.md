# Changelog

All notable changes to this project will be documented in this file.

## [0.5.9] - 2026-07-08

### Fixed
- **The interrupt card now names the real action instead of "an action" (gh #44).** The
  VS Code extension rendered the HITL approval card from `action_requests[0].tool`, but the
  sidecar emits the standard `HumanInterrupt` action request as `{"action": <tool>, "args":
  {...}}` (key `action`, never `tool`) — so the card always fell back to *"The agent wants
  to run **an action**"* and could never show e.g. `delete_file`. The extension now reads
  `.action` (falling back to `.tool`, then a generic label). The masking sidecar test that
  drove a fictional `{"tool": ...}` shape was switched to the real `HumanInterrupt` shape.

### Fixed
- **`--show-config` no longer crashes on a cp1252 stdout when a resolved value has a
  non-Latin-1 character (gh #42).** The VS Code extension spawns the sidecar over a cp1252
  pipe (and a Western-Windows console is cp1252 too), both with the `strict` error handler
  — so a resolved value containing a CJK/Cyrillic character (an agent spec, or a project
  path under a non-Latin-1 folder) made the bare `print(cfg.describe(...))` die with a raw
  `UnicodeEncodeError` and emit nothing. The protocol path was already ASCII-safe; the
  `--show-config` text path now degrades unrepresentable characters to escapes instead of
  crashing (full fidelity is preserved on a UTF-8 stdout).

### Fixed
- **The standard HumanInterrupt shape no longer crashes an interrupt turn, and no
  asyncio warning leaks to stderr (gh #40).** Driving the sidecar with the list of
  `HumanInterrupt` dicts that deepagents / langchain HITL actually emit
  (`[{"action_request": {...}, "config": {...}}, ...]`) used to yield
  `error: 'list' object has no attribute 'get'` instead of an interrupt frame. The root
  cause was in core's `on_interrupt` handler — fixed in **langstage-core 1.0.10** (now the
  minimum pin), which normalizes the HumanInterrupt list, our own keyed dict, and a plain
  dict. The sidecar now surfaces a real `interrupt` frame with `action_requests`
  populated and resumes.
- **(Defect 2, this repo)** `stream_events_sync` closed its event loop without draining
  the async generator, so an exception escaping mid-stream (or a consumer stopping early)
  left a pending task alive and asyncio logged *"Task was destroyed but it is pending!"*
  to stderr — bad for a stdio sidecar. It now `aclose()`s the generator and
  `shutdown_asyncgens()` before closing the loop.

## [0.5.6] - 2026-07-05

### Fixed
- **`--agui` no longer hard-crashes the sidecar (gh #38).** The 0.5.0 CHANGELOG
  promised the removed `--agui` flag would be *accepted-and-ignored for one release*
  so existing launch configs don't break — but the shim was never implemented, so
  passing `--agui` made `argparse` reject it (`unrecognized arguments`) and the
  sidecar exited **2 without ever emitting `ready`**. It is now accepted and silently
  ignored (hidden from `--help`), honoring the documented promise. The
  `LANGSTAGE_VSCODE_AGUI` env half already behaved.

## [0.5.5] - 2026-07-04

### Fixed
- **The sidecar now operates the agent from the workspace as its cwd (ADR 0006).**
  After resolving the agent spec, it `chdir`s into the resolved workspace, so a
  bring-your-own agent's raw relative file writes (`Path("out.txt").write_text(…)`)
  land in the workspace instead of the launch dir — matching the cli. The spec is
  resolved first, so a relative `-a ./x.py:graph` still resolves against the
  invocation cwd.

## [0.5.4] - 2026-07-04

### Fixed
- **The sidecar accepts `-a` as a short alias for `--agent` (dogfood).** The cli uses
  `-a`, so `python -m langstage_vscode -a my_agent.py:graph` used to fail with
  "unrecognized arguments: -a" — muscle memory from the cli examples broke on vscode.
  Both `-a` and `--agent` now work.

## [0.5.3] - 2026-07-04

### Fixed
- **A `decision` command with an empty `decisions: []` is now rejected with an
  `error` frame (gh #33).** The sidecar used to `ack` it and drive a full, spurious
  agent turn — with no interrupt to resume — instead of the promised `error` frame,
  inconsistent with the `message` path that rejects empty content. It now errors
  (`decision requires a non-empty 'decisions' list`) like the docs advertise.

## [0.5.2] - 2026-07-03

### Changed
- **Workspace root is now handed to the agent through the shared
  `core.apply_workspace()` (ADR 0005).** Replaces the two manual
  `os.environ["LANGSTAGE_WORKSPACE_ROOT"] = ...` blocks (the real run path and
  `--selfcheck`) with the one source of truth. Same behavior — the resolved
  workspace still reaches the agent via the canonical + legacy env vars (the gh #19
  fix is preserved) — plus it's now recorded as the active workspace for
  `core.workspace_root()` and the dir is ensured. No `chdir` (the sidecar loads a
  possibly-relative spec right after, which must resolve against the invocation
  cwd). Requires `langstage-core>=1.0.7`.

## [0.5.1] - 2026-07-03

### Fixed
- **README no longer claims `--demo` needs the `[demo]` extra (gh #30).** Since
  0.5.0 the base deps pull `langstage-core[agui]`, which brings `langgraph`, so
  the keyless echo stub runs on a bare `pip install langstage-vscode` — verified
  clean-room. Dropped the stale "needs the `[demo]` extra / base ships only the
  sidecar" notes from both README install blocks, and marked the `[demo]` extra
  a redundant no-op alias in `pyproject.toml` (kept so existing install commands
  still resolve). Also fixed a stale `langgraph_stream_parser.demo.stub` mention
  in that extra's comment (the module is `langstage_core.demo.stub`).

## [0.5.0] - 2026-07-02

### Changed
- **AG-UI is now the sidecar's only streaming path (ADR 0003).** The built-in
  `StreamParser` path is gone; every turn streams through `langstage-core`'s
  in-process AG-UI adapter, emitting the exact same `event_to_dict`-shaped frames
  the TS extension already renders — so the wire and the extension are unchanged.
  The `--agui` flag and `LANGSTAGE_VSCODE_AGUI` env are removed (they toggled a
  path that no longer exists); both are accepted-and-ignored for one release so
  existing launch configs don't break.
- **Repointed to `langstage-core` 1.0** (the rename of `langgraph-stream-parser`;
  ADR 0003). The AG-UI runtime (`ag-ui-langgraph[fastapi]` + uvicorn, via core's
  `[agui]` extra) moved into **base dependencies**: since AG-UI is the only path,
  a bare `pip install langstage-vscode` must be able to run a turn. The `[agui]`
  extra is now a redundant no-op alias, kept so existing install commands resolve.

### Removed
- `StreamParser`/`event_to_dict` imports, the `_run_turn` parser turn function,
  and the `--agui`/env branching in `run()` and `main()`. The command/event loop,
  frame vocabulary, `--demo`, `--selfcheck`, and `--show-config` are unchanged.

## [0.4.10] - 2026-07-01

### Changed
- **Internal dedupe (ADR 0002):** the `--agui` path's AG-UI→`event_to_dict`
  mapping now delegates to the core's `langgraph_stream_parser.agui.iter_event_frames`
  (0.6.16), shared with the web `SessionAdapter`, instead of carrying its own copy.
  Behavior is unchanged (same frames; tests still pass) — the mapping just has a
  single source of truth so rendering fixes land once. Core floor → `>=0.6.16`.

## [0.4.9] - 2026-07-01

### Added
- **Experimental `--agui` sidecar path (ADR 0002).** The sidecar can stream
  through the official in-process `ag-ui-langgraph` adapter instead of the
  built-in event parser, opt-in via `--agui` or `LANGSTAGE_VSCODE_AGUI=1`. It
  emits the **same `event_to_dict` JSON frames** (`content`/`tool_start`/
  `tool_end`/`interrupt`/`complete`/`error`), so the TS extension's dispatcher is
  **unchanged**. Text, tool calls/results, and interrupts (display + resume via
  the adapter's `forwarded_props.command.resume`) all reach frame parity with the
  default path. Requires the `agui` extra: `pip install "langstage-vscode[agui]"`.
  The default path is untouched. Third surface of the family's AG-UI migration
  (after `langstage-cli` and `langstage-jupyter`).

## [0.4.8] - 2026-06-29

### Added
- **`--selfcheck` (alias `--smoke`): preflight the spawned interpreter + agent
  spec before the first chat message.** Loads the configured agent (or the demo
  stub) and asserts it's a runnable graph — failing with a precise message that
  names the spec and what it actually loaded, instead of a cryptic first-message
  `'...' object has no attribute 'stream'` — then drives one real turn and exits
  0/non-zero. `--json` emits a machine-readable verdict for the extension to
  consume. (Found by the dogfood routine, gh #21.)

## [0.4.7] - 2026-06-28

### Fixed
- **The `--workspace` override never reached the agent.** The sidecar handed the
  workspace to the agent via `os.environ.setdefault`, a no-op when
  `LANGSTAGE_WORKSPACE_ROOT` was already exported — so `--workspace` was silently
  dropped (the agent read the stale env value) even though `--show-config`
  reported the override as winning. It now assigns the resolved value
  unconditionally. (Found by the dogfood routine, gh #19.)

## [0.4.6] - 2026-06-27

### Fixed
- **The legacy `deepagent_vscode` alias dropped the old package's public API.**
  The rename promised "existing imports keep working," but the alias re-exported
  only the `sidecar` submodule — so `from deepagent_vscode import main, run` and
  `deepagent_vscode.__version__` (all in the old package's `__all__`) raised
  `ImportError`/`AttributeError`. The alias now re-exports `main`, `run`, and
  `__version__` from `langstage_vscode` (with `__version__` deriving from
  installed metadata, per #9), so old programmatic consumers keep working through
  the transition window. (Found by the dogfood routine, gh #17.)

## [0.4.5] - 2026-06-25

### Fixed
- **`--show-config` advertised inert server/UI keys on the stdio sidecar.** It
  listed `host`, `port`, `debug`, and `title` (inherited from the shared
  `HostConfig`) with full `LANGSTAGE_*` / TOML source attribution — but this
  surface is a pure stdio sidecar that never opens a socket or renders a UI, so
  those four do nothing. `--show-config` now shows only the keys the sidecar
  honors (`agent_spec`, `workspace_root`), via core's new
  `describe(omit_keys=…)` (bumps the core floor to `>=0.6.11`). (Found by the
  dogfood routine, gh #14.)

## [0.4.4] - 2026-06-22

### Fixed
- **`--demo` was needlessly heavy and errored misleadingly on a base install.**
  The `[demo]` extra pulled the entire `deepagents` ML stack (~30 packages incl.
  `anthropic`/`google-genai`) just to obtain `langgraph` — but the demo agent is
  the keyless echo stub, which needs only `langgraph`. `[demo]` now pulls core's
  lightweight `langgraph-stream-parser[stub]` extra instead (verified: a clean
  `pip install "langstage-vscode[demo]"` installs `langgraph` with **no**
  `deepagents`, and the stub agent loads). And the base core floor is now
  `>=0.6.10`, so a base-install `--demo` (without the extra) gets core's honest
  "install the [stub] extra" error instead of the old false "every deep-agent
  surface already installs them" message. (Found by the dogfood routine.)

## [0.4.3] - 2026-06-21

### Fixed
- **`tool_end` reported `name="unknown"`** even though `tool_start` (same id)
  carried the tool name. Fixed upstream in `langgraph-stream-parser` 0.6.7; bumped
  the core pin to `>=0.6.7,<0.7` (base + `[agui]`) to deliver it.
- **`--help` em-dash mojibaked on a default Windows (cp1252) console.** Replaced
  the non-ASCII em-dash in the `--demo` help with ASCII so `--help` renders cleanly.

### Added
- **`--version`** flag on the sidecar (`langstage-vscode-sidecar --version`),
  mirroring `langstage-agui` — it previously errored with `unrecognized arguments`.

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
