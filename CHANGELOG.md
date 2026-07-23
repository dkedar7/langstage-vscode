# Changelog

All notable changes to this project will be documented in this file.

## [Extension 0.3.1] - 2026-07-23

_VS Code extension only (`extension/package.json` 0.3.0 -> 0.3.1). No sidecar / PyPI
release — the Python `langstage-vscode` package is unchanged at 0.5.19._

### Fixed
- **"Stop" in the chat now cancels the turn cooperatively instead of killing the sidecar, so
  conversational memory survives a cancel (gh #69).** This is the extension (client) half of gh #67,
  whose sidecar half shipped in 0.5.19 above. The extension's `runTurn()`
  (`extension/src/extension.ts`) still handled `onCancellationRequested` by calling
  `disposeSidecar()` — killing the long-lived process and wiping its in-process `MemorySaver` — under
  a comment that became **factually false** once 0.5.19 landed (_"The stdio protocol has no per-turn
  cancel command…"_). So hitting the chat "stop" button mid-turn silently erased the whole
  conversation's memory, the exact harm gh #67 set out to fix. Now `onCancellationRequested` writes
  the cooperative `{"type":"cancel","session_id":"vscode"}` command to the live sidecar over the same
  stdin JSON-lines writer used for `message`/`decision`, and lets the resulting `cancelled` ->
  `turn_end` frames end the turn through the normal frame loop **without tearing the process down** —
  so the session, its `thread_id`, and its checkpointer stay alive and the next turn reuses the same
  warm process (same `session_id` -> same LangGraph `thread_id`) with memory intact. `disposeSidecar()`
  is now reserved for genuine teardown (extension deactivate, config change, a new conversation, and a
  sidecar crash), not a per-turn cancel; if the sidecar's stdin is already gone when a cancel fires,
  it falls back to a clean teardown so the turn still ends. The now-false comment is replaced with an
  accurate one, and the `cancelled` frame is documented as an intentionally silent terminal frame in
  the dispatcher (not mistaken for an error or unknown frame).

## [0.5.19] - 2026-07-23

### Added
- **A cooperative per-turn `cancel` command: stop an in-flight turn WITHOUT tearing down the
  session and its in-process checkpointer (gh #67).** The stdio protocol had exactly three
  client->sidecar commands — `message`, `decision`, `shutdown` — and **no way to abort a running
  turn**. So the only way the extension could honor a "stop" in the chat UI was to **kill the whole
  sidecar process** (`disposeSidecar()`), which throws away the long-lived session and its
  in-process `MemorySaver` — cancelling one slow turn silently reset the entire conversation's
  memory (the very thing gh #54 keeps one process alive to preserve), and paid a full interpreter +
  agent reload on the next turn. The extension's own code said as much in a comment. Now a fourth
  command aborts just the current turn:

  ```jsonc
  {"type": "cancel", "session_id": "s1"}
  ```

  - **It stops the streaming turn cooperatively.** The raw stdio loop is single-threaded and, while
    pumping a turn's frames, never reads stdin — so on that path a background reader now surfaces a
    `cancel` mid-turn. Between frames the turn checks for it and, when it arrives, closes the AG-UI
    stream; the generator's existing `finally: aclose()` cancels the pending ag-ui run task (the
    same early-stop that already ran on process teardown, gh #40), leaving the graph's
    session/thread and checkpointer **untouched**.
  - **It emits a distinct terminal frame.** A cancelled turn is `cancelled -> turn_end`, with **no**
    `complete` — mirroring the "there is no `complete` on a non-success terminal turn" contract, so a
    client never confuses a cancel with a clean turn or an error.
  - **The session survives, so the next turn keeps memory.** The process is not torn down; a
    subsequent `message` on the same `session_id` resumes with prior context intact (verified
    end-to-end: warm up a turn, cancel a long streaming turn, then a third turn still remembers the
    first).
  - **A `cancel` with nothing in flight is refused cleanly** — an `error` frame
    (`no turn in progress for session '…'`), consistent with how the `decision`/`message` guards
    reject un-actionable commands (cf. gh #33/#65), rather than acking or crashing (it used to be an
    `unknown command type: 'cancel'` error). This ships the sidecar half; the extension can now send
    `cancel` from `onCancellationRequested` instead of `disposeSidecar()`, so "stop" stops the turn
    without erasing the conversation.

### Fixed
- **A well-formed `decision` sent with no pending interrupt no longer drives a spurious turn on the
  raw stdio path (gh #65) — the non-empty sibling of gh #33.** gh #33 made a `decision` with an
  empty `decisions: []` error instead of ack + drive a turn, but it only closed the *shape* half:
  the guard checked that `decisions` is a non-empty list and stopped there. A perfectly well-formed
  `decisions: [{"type": "approve"}]` arriving on a thread that was **never interrupted** still
  slipped through and was ack'd + driven — a spurious empty-input turn that, depending on the agent,
  either reported a false `complete` (the client is told the decision was applied when nothing was
  resumed) or leaked a raw internal error (e.g. `IndexError` from a node reading `messages[-1]`).
  That is exactly the "there's no interrupt to resume, so it must error" invariant the gh #33 code
  comment *states* but did not enforce for the non-empty case. The raw `run()` loop now tracks
  pending-interrupt state **per `session_id`/thread**, off the frames it emits — the raw-protocol
  analogue of `_ReplSink.pending_interrupt` (gh #63): a turn that ends on an `interrupt` leaves that
  session PENDING, and the next turn on it (a resume that completes, or a fresh message) clears it.
  A `decision` for a session with nothing pending now emits `error`
  (`no interrupt pending for session '…'`) and drives no turn — no ack, no content, no false
  `complete`. The `#63` `--repl` decision layer already guarded this; the raw path now matches. The
  extension is unaffected (it only sends a `decision` in response to an interrupt), but a client
  hand-driving the documented protocol reaches it directly.
- **Docs: the interrupt->decision `--json` trace in the README and CHANGELOG now shows the two
  `complete` frames the runtime actually emits (gh #66).** The shipped trace read
  `ready -> ack message -> interrupt -> turn_end -> ack decision -> content -> turn_end` and claimed
  it was "identical to the hand-written raw-protocol transcript" — but the runtime emits a `complete`
  after **both** the `interrupt` turn and the resumed `content` turn, so the real sequence is
  `ready -> ack message -> interrupt -> complete -> turn_end -> ack decision -> content -> complete -> turn_end`
  (regenerated by running the round-trip against a real HITL agent on this branch, gh #61 lesson).
  The gh #58 docs had it right with `complete`; the gh #63 rewrite dropped it. Both traces are
  corrected, and the `## Sidecar protocol` note now spells out that an **interrupt** turn emits
  `complete` too (paused != produced-a-reply — detect the pause via the `interrupt` frame, not the
  absence of `complete`), alongside the new `cancelled` terminal shape.

## [0.5.18] - 2026-07-19

### Added
- **`--repl` can now ANSWER a human-in-the-loop interrupt, completing the `interrupt` ->
  `decision` round-trip from the CLI (gh #63).** This is the deferred **part 2** of gh #58 and
  it **supersedes the "intentionally still future work" note in 0.5.16 below** — that note was
  accurate when it shipped and is no longer true. 0.5.16 made the pause *visible* (a stderr
  notice, and exit `2` for one-shot `--message`), but you still could not *answer* it: the next
  `--repl` line started a **fresh `message` turn**, which — for the common approval agent — just
  re-interrupts, so an `approve` line looked accepted while the pending interrupt was never
  resumed. Completing the round-trip meant dropping to hand-written NDJSON over the raw stdio
  protocol with one process kept alive and matching `session_id`s: exactly the friction
  `--message`/`--repl` exist to remove, and the last sidecar capability with no CLI verification
  path. Now a turn that ends on an `interrupt` puts the session in **decision mode**, and the
  next line becomes a `{"type": "decision", ...}` command on the **same** `session_id` (hence the
  same LangGraph `thread_id`), so it lands on *that* thread's pending interrupt and the resumed
  reply streams through the same sink:

  ```console
  $ printf 'do it\napprove\n:quit\n' | langstage-vscode-sidecar --agent ./hitl.py:graph --repl
  interrupt: agent paused awaiting a decision
    action: confirm   allowed: reject | edit | respond | approve
    answer it here: `:decision <verb>` (or a bare `<verb>`) using a verb above
    payloads: reject [<text>] | edit <json> | respond <text>
  resumed with: {'decisions': [{'type': 'approve'}]}
  ```

  The design calls, all aimed at the failure mode that would be worse than the old honest
  limitation — silently treating a message as a decision, or a decision as a message:

  - **While an interrupt is pending, every line is an answer attempt** (`:quit` excepted, so
    there is always a way out). A line that isn't a valid decision is **refused on stderr and
    re-prompted with the interrupt left pending** — never silently downgraded to a `message`
    (the bug) and never swallowed. The note names what you typed, why it was refused, and the
    verbs this interrupt actually allows, so the next line still answers it.
  - **`:decision <verb> [payload]`** is the explicit form, in the same `:`-prefixed command
    namespace as `:quit`, and it is what the notice tells you to type. A **bare `<verb>`** also
    works, but *only* while an interrupt is pending — the one state where it is unambiguous;
    outside decision mode `approve` is ordinary chat text and still drives a normal turn, and a
    stray `:decision` is refused with "no interrupt is pending" rather than chatted at the agent.
  - **The accepted verbs come from the interrupt frame itself** (`allowed_decisions`), never a
    hard-coded list: an interrupt that advertises only `reject | approve` (the `allow_ignore` +
    `allow_accept` shape) refuses `edit` and never offers it in the notice. Payload grammar
    follows the canonical langchain HITL decisions — `approve`, `reject [<text>]`,
    `respond <text>`, `edit <json>` — where free text becomes `message` and a JSON object is
    merged in, so the full typed shapes (`edit {"edited_action": {...}}`) are reachable.
  - **`--json` composes unchanged and is the machine-readable proof.** The answer line now emits
    `ack decision` (not the old `ack message` + a second `interrupt`), so the trace reads
    `ready -> ack message -> interrupt -> complete -> turn_end -> ack decision -> content -> complete -> turn_end` —
    identical to the hand-written raw-protocol transcript. (Both turns emit `complete`: an
    interrupt turn is `interrupt -> complete -> turn_end`, paused but still `complete`. An earlier
    revision of this line dropped the two `complete` frames; corrected in 0.5.19, gh #66.) Refusals
    stay on stderr, so the NDJSON stream carries no frame for a rejected line.
  - **The interrupt notice now tells you what to type.** In `--repl` it prints the inline answer
    syntax; one-shot `--message` keeps the old "resume by sending a `decision` command" line,
    since that process exits at `turn_end` and genuinely cannot answer. Still ASCII-only, same
    cp1252 rule. At a TTY the prompt also changes to `decision> ` while a decision is pending.

### Changed
- **`--repl` exit codes are now a three-way signal, matching `--message` (gh #63).** `0` on a
  clean session — **including a turn that interrupted and WAS answered** — `1` if the agent could
  not start at all (e.g. a non-runnable spec), and **`2` if the session ends (EOF/`:quit`) with an
  interrupt still pending**, i.e. never answered. Under 0.5.16 `--repl` always exited `0` on an
  interrupt because it had no way to answer one, so the pause was purely informational; now that
  answering is a first-class REPL action, walking away from a pending interrupt is a real outcome
  and gets the same "paused awaiting a decision" code `--message` has carried since 0.5.16. This
  makes the whole round-trip scriptable in one line:
  `printf 'do it\napprove\n' | ... --repl; echo $?` -> `0` proves it completed, `2` proves it did not.

## [0.5.17] - 2026-07-18

### Fixed
- **The PyPI project page is no longer blank — the README ships with the package (gh #60).**
  `[project]` in `pyproject.toml` had no `readme` key, so hatchling never set a long
  description or `Description-Content-Type`, and <https://pypi.org/project/langstage-vscode/>
  rendered nothing: no install command, no Quickstart, no sidecar-protocol table. An adopter's
  natural first stop carried none of the 12 KB README sitting in the repo, making this the one
  LangStage stage whose docs never reached PyPI. Adding `readme = "README.md"` restores parity
  with every sibling stage. A packaging test now asserts the key is present and points at real
  content, so the omission cannot silently return.

  This also removes the trap described in #61: because PyPI carried no README, adopters were
  pushed to the repo `HEAD` README, which documented behavior newer than the latest *installable*
  release. With 0.5.17 published, the rendered PyPI docs and the shipped code match again.

## [0.5.16] - 2026-07-16

### Fixed
- **The CLI turn-drivers are now interrupt-aware: `--message` and `--repl` no longer render a
  turn that ends on a HITL `interrupt(...)` as a silent blank exit-0 (gh #58).** The family
  grew a verification flag for every question a user asks before wiring up chat — `--show-config`
  (what's resolved?), `--selfcheck` (is it healthy?), `--message` (what does it say once?),
  `--repl` (does it remember?) — but the **one documented sidecar capability with no CLI
  verification path** was the human-in-the-loop `interrupt` → `decision` round-trip, and it was
  precisely the one that broke worst: when an agent hit a LangGraph `interrupt(...)`, both
  `--message` and `--repl` printed **nothing** to stdout and stderr and exited **0** —
  indistinguishable from an empty reply or a silent no-op, even though the runtime *did* pause
  (the raw `interrupt` frame was there all along on the `--json` stream). Now an interrupt turn
  is **surfaced**: in human mode a concise notice goes to **stderr** (keeping stdout the clean
  reply channel, exactly like the `error` path) naming the pending action and the decisions the
  frame advertises — e.g. `interrupt: agent paused awaiting a decision` / `action: confirm
  allowed: reject | edit | respond | approve`; in `--json` mode the raw `interrupt` frame streams
  on stdout as before, so a consumer keys on `type == "interrupt"`. One-shot **`--message` also
  gains a distinct exit code: `2` when the turn ends on an interrupt** (vs `0` on a clean reply
  and `1` on an `error` frame), so the pause is scriptable/CI-gateable instead of a silent
  exit-0. The notice is ASCII-only so it survives a cp1252 console unmangled (the same rule the
  `--help` and `_write_safe` guards enforce), and both drivers reuse the shared streaming sink so
  the two can't drift. `--repl` surfaces the interrupt the same way but, like a per-turn `error`
  in a live session, keeps the interactive session alive (clean EOF/`:quit` still exits `0`).
  This makes the last unverifiable sidecar capability verifiable in ten seconds:
  `langstage-vscode-sidecar --agent ./hitl.py:graph --message "do it"; echo $?` now prints the
  pending action and exits `2`. **Note:** *answering* an interrupt inline in `--repl` (mapping
  the next input line to a `decision` on the live session — the issue's separate part 2) is
  intentionally still future work; today the next line starts a fresh turn. Drive the `decision`
  command over the raw stdio protocol to complete the round-trip.
  > **Superseded in 0.5.18 (gh #63):** the deferral above no longer holds. `--repl` answers an
  > interrupt inline via `:decision <verb>` (or a bare verb), and `--repl` exits `2` — not `0` —
  > when a session ends with an interrupt still unanswered. See the 0.5.18 entry.

## [0.5.15] - 2026-07-15

### Added
- **An interactive `--repl` mode: the multi-turn companion to one-shot `--message`, so you
  can verify conversational memory straight from the CLI (gh #56).** The two turn-driving
  flags were both strictly single-turn — `--selfcheck` drives one fixed internal ping, and
  `--message` drives exactly one turn against a fresh `session_id: "once"` and exits — so the
  one thing the README's longest note warns is subtle to get right (does turn 2 remember
  turn 1?) had **no supported way to validate short of hand-writing the NDJSON protocol** and
  keeping one process alive with matching `session_id`s. `langstage-vscode-sidecar --repl`
  (or `--agent ./my.py:graph --repl`) now reads one prompt per input line, drives a turn, and
  prints the reply, looping over **one long-lived session** (a single fixed `session_id` →
  one LangGraph `thread_id` in one persistent `run()` loop) until EOF (Ctrl-D) or a `:quit`
  line. Because the whole session shares one thread — the exact per-conversation shape the VS
  Code extension uses (gh #54) — a checkpointer-backed agent **remembers prior turns**, making
  the checkpointer footgun observable in ten seconds ("tell it your name, ask on the next
  line"): against a `MemorySaver`-backed graph, `--repl` reports a rising message count across
  turns where a fresh-process-per-call `--message` stays flat. It is a thin front-end over the
  existing machinery — each input line becomes a `message` command and EOF/`:quit` becomes
  `shutdown`, fed lazily to the same `run()` loop; the reply is rendered by a `_ReplSink` that
  reuses one-shot `--message`'s live `content` streaming, `error`-to-stderr routing, and
  cp1252-safe `_write_safe` writes, adding only a per-turn `turn_end` boundary. No new protocol
  and no new rendering. `--json` composes with it (raw `event_to_dict` frames instead of
  assembled text), same as `--message`; the `> ` prompt is drawn to stderr only at a real TTY
  so stdout stays the clean reply channel and piped/scripted input isn't polluted. Blank input
  lines re-prompt instead of driving an empty-content turn; `--repl` and `--message` are
  mutually exclusive. Exit is `0` on a clean EOF/`:quit` termination (a per-turn `error` is
  surfaced to stderr but doesn't fail the interactive session) and non-zero only when the agent
  can't start a turn at all (e.g. a non-runnable spec), mirroring `--message`. This closes the
  loop on the family's config/agent story: `--show-config` (what's resolved?), `--selfcheck`
  (is it healthy?), `--message` (what does it say once?), and now `--repl` (does it *remember*?).

## [0.5.14] - 2026-07-14

### Fixed
- **The VS Code extension now keeps one sidecar process alive per conversation, so the
  documented multi-turn "conversational memory" actually holds (gh #54).** The extension
  spawned a **brand-new sidecar process for every `@langstage` message and killed it after
  one turn** (`extension/src/extension.ts`: `spawn(...)` per message, `proc.kill()` in the
  `finally`). An **in-process** checkpointer — `MemorySaver`, the one the README's memory
  note points at with `graph.compile(checkpointer=...)`, and the one virtually every
  LangGraph/`deepagents` example uses — lives inside that process, so it was wiped between
  turns: the agent had amnesia on turn 2 even though it was compiled with a checkpointer and
  the extension sent the same `session_id: 'vscode'` every time. The extension now spawns the
  sidecar on the first message of a conversation and **reuses that same long-lived process**
  (same `session_id` → same LangGraph `thread_id`) for every following turn, so an in-process
  `MemorySaver` persists across turns in chat. The process is restarted on a config change
  (`langstage.pythonPath` / `langstage.agentSpec`) and when a new conversation begins (so a
  fresh chat starts with a clean thread), and is torn down on deactivate. As a bonus this also
  removes the per-turn cold start (interpreter boot + agent import + AG-UI build) the old
  spawn-per-message path paid on every message. A turn cancellation kills the process (the
  stdio protocol has no per-turn cancel command) and the next turn respawns a fresh one.
  Tests lock the underlying sidecar contract: a checkpointer-backed agent driven through one
  persistent `run()` loop remembers prior turns (turn 2 sees turn 1's history), while a fresh
  checkpointer per turn — the old spawn-per-message shape — forgets. The README memory note is
  updated to describe the extension's long-lived process instead of implying an in-process
  checkpointer "just works" in chat.

## [0.5.13] - 2026-07-12

### Changed
- **One-shot `--message` now streams its output frame-by-frame instead of buffering the
  whole turn (gh #50).** `_run_once` used to run the entire `run()` loop into an in-memory
  `StringIO`, drain the whole turn, and only then print the assembled reply (or, with
  `--json`, replay all the buffered frames) — so on a slow or token-streaming agent the
  terminal looked frozen for the full turn, then dumped everything at once, and
  `--message ... --json | jq` saw nothing until the end. It now writes to a small streaming
  sink handed to `run()` in place of the buffer: human mode types each `content` frame's text
  out live (no trailing newline until the turn ends), and `--json` forwards each
  `event_to_dict` frame the instant it's emitted (error frames included) — a genuine
  streaming NDJSON source. The flag's contract is unchanged: stdout stays the clean reply
  channel, `error` frames go to stderr in human mode, and the exit code is still `0` on a
  clean turn / non-zero on an `error` frame. The cp1252-safe reply handling from gh #51 is
  preserved (every write still routes through `_write_safe`).

## [0.5.12] - 2026-07-12

### Fixed
- **One-shot `--message` no longer crashes on a cp1252 stdout when the agent's reply
  contains a non-Latin-1 character (gh #51).** The `--message` human-mode path printed the
  assembled reply with a bare `print(reply)`, so on a cp1252 (strict) stdout — a Western-
  Windows console, and the pipe the VS Code extension spawns the sidecar on — any reply with
  a character outside cp1252 (a ✅/CJK/emoji an LLM emits routinely) died with an uncaught
  `UnicodeEncodeError` and a full traceback, losing the reply and exiting `1`. This is the
  same bug class already fixed for `--show-config` in gh #42, whose guard was never applied to
  the newer `--message` path (added in 0.5.11). Both raw-text stdout paths — plus the
  `--message` error-frame writes to stderr, which `PYTHONIOENCODING=cp1252` makes strict too —
  now route through one shared `_write_safe` helper that degrades unrepresentable characters
  to backslash escapes instead of crashing (full fidelity is preserved on a UTF-8 stream), so
  the two guards can't drift again.

## [0.5.11] - 2026-07-10

### Added
- **A one-shot `--message` / `--prompt` flag: drive a single turn and print the reply,
  without hand-crafting NDJSON (gh #48).** `--selfcheck` validates the runtime with a fixed
  internal ping and prints only an `OK:`/`FAIL:` verdict; the only way to see your agent's
  actual answer to a prompt of your choosing was to drive the stdio protocol by hand
  (`printf '{"type":"message",...}' '{"type":"shutdown"}' | python -m langstage_vscode ...`).
  `langstage-vscode-sidecar --agent ./my.py:graph --message "summarize the repo"` now loads
  the agent, drives one turn, prints the **assembled reply**, and exits `0` (clean) /
  non-zero (on an error frame) — no `shutdown` handshake. Pair with `--json` to emit the raw
  `event_to_dict` frames instead, for scripting. Honors the same config chain and
  `--workspace` as a normal run; internally it's the same `run()` loop `--selfcheck` uses.

## [0.5.10] - 2026-07-09

### Fixed
- **A spec that loads a non-runnable object crashed the sidecar with no `error` frame
  (gh #46).** When `--agent <spec>` loaded successfully but resolved to something that
  isn't a runnable `CompiledGraph` (a factory function, an uncompiled `StateGraph`, a bare
  value), the sidecar emitted `{"type": "ready"}` and then died with an uncaught
  `AttributeError` from deep inside `ag-ui` — never emitting the documented
  `{"type": "error", ...}` frame the extension keys off. The runtime path now applies the
  same runnable-graph guard `--selfcheck` already uses, before building the agent, and
  surfaces the identical actionable message (`... not a runnable graph (no `.stream`).
  Point the spec at the compiled graph attribute, e.g. `module:graph`.`). The check is
  factored into one helper shared by both paths so they can't drift, and the agent-build
  `except` was broadened so any build failure becomes an `error` frame instead of a crash.

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
