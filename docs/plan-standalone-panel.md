# Build plan: standalone LangStage panel

Implements [ADR 0001](adr/0001-standalone-panel.md): a webview chat panel over the existing
stdio sidecar that works in VS Code, Cursor, VSCodium, Windsurf and code-server, alongside the
`@langstage` Copilot participant. Sizes are focused engineering days (S ≤ 2, M 3–5, L > 5).
**Total: roughly 17–26 days.** Each milestone is its own PR and leaves `main` releasable.

## Target architecture

```
┌──────────── webview (vscode-webview:// origin, strict CSP) ────────────┐
│ React UI: ConversationList · Transcript · ToolCard · ReasoningBlock ·  │
│ TodoList · InterruptCard · Composer (send / stop) · StatusLine         │
│ reducer(frames) → view model          acquireVsCodeApi().postMessage   │
└───────────────────────────────▲───────────────────┬────────────────────┘
          host → webview: frame │ status │ restore  │ webview → host: send │ decide │ cancel │ …
┌───────────────────────────────┴───────────────────▼────────────────────┐
│ extension host: PanelController ── ConversationStore (storageUri JSON) │
│                 ChatParticipant (only if vscode.chat exists)           │
│                 SidecarClient × 2 (one per surface)                    │
└───────────────────────────────▲───────────────────┬────────────────────┘
                     NDJSON frames (stdout)          │ NDJSON commands (stdin)
┌───────────────────────────────┴───────────────────▼────────────────────┐
│ python -m langstage_vscode --workspace <root>   (unchanged protocol)    │
└─────────────────────────────────────────────────────────────────────────┘
```

### Webview ⇄ host message protocol (new, extension-internal)

Versioned by a `v: 1` field and defined once in `extension/src/shared/panelProtocol.ts`, which both
bundles import.

| Direction | Message | Payload |
|---|---|---|
| webview → host | `ui/ready` | — (host answers with `restore`) |
| webview → host | `send` | `conversationId, text` |
| webview → host | `decide` | `conversationId, decisions[]` (built with `hitl.ts` `buildDecisions`) |
| webview → host | `cancel` | `conversationId` |
| webview → host | `conversation/new` · `/switch` · `/delete` · `/rename` | `conversationId, title?` |
| webview → host | `openExternal` · `openSettings` · `restartSidecar` | `url?` |
| host → webview | `restore` | conversations[], active id, transcripts (reduced), sidecar status |
| host → webview | `frame` | `conversationId, frame` (a sidecar frame, verbatim) |
| host → webview | `turn/queued` · `turn/started` · `turn/ended` | `conversationId` |
| host → webview | `status` | `starting · ready · failed(error, stderrTail) · stopped`, sidecar version |

The host owns truth: the conversation ↔ `session_id` map, the persisted transcripts, and the pending
interrupt. The webview can be torn down and rebuilt at any time from `restore`.

### Sidecar protocol

**No change needed for v1.** One optional additive change (M6, sidecar 0.6.x): `ready` gains
`{"version": "...", "protocol": 1}` so the panel can say "sidecar too old, run `pip install -U
langstage-vscode`". Older sidecars omit it, and the panel then treats the protocol as 1 and
doesn't warn. README "Sidecar protocol" is updated only if that ships.

## Milestones

### M0: host refactor, cross-editor safety (S, 1–2 d)
- `extension/src/sidecar.ts`: extract `SidecarClient` from `extension.ts`. It covers spawn, `ready`
  gating, startup-error capture (gh #131), stderr tail, routing frames by `session_id` to
  per-turn listeners, `cancel`, `dispose`, and a turn queue.
- `extension/src/participant.ts`: the existing handler, moved unchanged onto its own
  `SidecarClient`.
- `extension.ts`: `if (vscode.chat?.createChatParticipant)` guard. Today activation throws where
  the chat API is missing.
- `package.json`: `capabilities.untrustedWorkspaces: { supported: false }` (the agent is workspace
  code), plus `activationEvents` for the view.
- Tests: `node --test` for `SidecarClient`, driven by a fake child process that replays NDJSON
  fixtures. The existing `hitl.test.ts` stays green.

### M1: panel skeleton (M, 3–4 d)
- `package.json` `contributes`:
  - `viewsContainers.activitybar` → `langstage`;
  - `views` → `langstage.chat` (type `webview`);
  - commands `langstage.panel.focus`, `langstage.newConversation`, `langstage.restartSidecar`.
- `extension/src/panel/PanelController.ts`: `registerWebviewViewProvider` with
  `retainContextWhenHidden: true`. The HTML carries:
  - a CSP: `default-src 'none'; script-src 'nonce-…'; style-src ${cspSource} 'unsafe-inline'; img-src ${cspSource} data:; font-src ${cspSource}`;
  - `localResourceRoots` limited to `dist/webview`;
  - asset URLs built with `asWebviewUri`.
- `extension/webview/` (new): `main.tsx`, `vscodeApi.ts`, `state/reducer.ts`, `Composer.tsx`,
  `Transcript.tsx`, `StatusLine.tsx`. Plain text streaming, Send, Stop (maps to `cancel`), Enter or
  Shift+Enter.
- Status line covers:
  - the "no agent configured" state, with an **Open settings** link and a **Try the demo** link
    that sets `--demo=tools` for the session;
  - startup errors, shown verbatim.
- Build:
  - `esbuild` to `dist/webview/main.js` (iife, minified);
  - the extension stays on `tsc`, or also moves to esbuild so `node_modules` can be dropped
    from the VSIX;
  - `npm run build` builds both bundles.
- Measure the bundle size of React 19 against Preact (ADR open question 6). Budget: webview under
  400 KB minified.

### M2: rich rendering, ported from the web frontend (M, 3–5 d)
Port the prop-driven components from `langstage/frontend/src/components`, re-themed with `--vscode-*`
CSS variables (no Tailwind), with a header comment naming the source file and commit:
- `MessageBubble` → `Message.tsx`:
  - rendered with `react-markdown` and `remark-gfm`;
  - **no `rehype-raw`**: agent output is untrusted, so HTML is escaped;
  - links go through `openExternal`, which the host confirms;
  - code blocks get a copy button;
  - a `message_id` change starts a new paragraph (gh #108).
- `ToolCallCard` → `ToolCard.tsx`:
  - collapsible;
  - shows name, args JSON, result (truncated with an expand control), status, and `duration_ms`;
  - a spinner while the tool runs;
  - pairs `tool_start` with `tool_end` by `id`.
- **New** `ReasoningBlock.tsx`: collapsed by default and streams in place. The web app has no
  equivalent.
- `TodoPanel` → `TodoList.tsx`: one live checklist per conversation, updated in place from each
  `extraction` `todos` frame, not re-emitted.
- `useAgentStream`'s dispatch → `state/reducer.ts`: a pure `(state, frame) → state` function,
  unit-testable. It ignores unknown frame types and keys.
- Errors are shown inline, with `traceback` in a collapsible when present.

### M3: human-in-the-loop in the panel (S–M, 2–3 d)
- `InterruptCard.tsx`, ported from `InterruptDialog`, shows each action from `summarizeActions`
  (name, description, args).
- One button per verb in `allowed_decisions`:
  - **Approve**;
  - **Reject**, with an optional reason;
  - **Respond**, with an inline textarea;
  - **Edit**, with an inline JSON editor prefilled with the action's args and checked as JSON
    before sending.
- Custom verbs are listed with a generic button that sends `{type: verb}`.
- Uses `hitl.ts` `buildDecisions` unchanged, imported by the webview bundle. The sidecar still
  validates with `normalize_decision`.
- A refused decision (`error → turn_end` with no `ack`) keeps the card active and shows the
  error.
- While paused, the composer shows "Answer the request above" and a new message is blocked.
  This mirrors the sidecar's rule (gh #134) without depending on it.

### M4: conversations and persistence (M, 3–4 d)
- `ConversationStore.ts` keeps its index, titles, a `session_id` per conversation
  (`vscode-<uuid>`) and reduced transcripts in `context.storageUri` JSON. That location is
  per-workspace and per-machine, and is not synced.
- `ConversationList.tsx` supports new, switch, rename and delete. Delete removes the transcript
  only; there is no checkpointer deletion in v1.
- Title: the first line of the first user message.
- **One turn at a time per sidecar**:
  - a message sent in conversation B while A is streaming shows as *queued* (`turn/queued`);
  - the host queues it rather than relying on the sidecar's stash;
  - Stop cancels only the active conversation.
- **Restore after a window reload.** On reload the transcript comes back, but the sidecar is
  new. If the checkpointer is the in-memory default, a restored conversation shows the banner
  *"The agent doesn't remember this conversation (in-memory checkpointer). Configure a
  durable checkpointer to keep memory across restarts."* To detect the default, read
  `--show-config --json` once at startup.
- Config changes (interpreter, agent spec) restart the panel's sidecar. Status shows
  "restarted: conversation memory reset" under the same rule.

*Shipped in extension 0.6.0 (M3–M5).* Deviations: the `in-memory checkpointer` banner is an
inline note on every restored conversation, worded "may not remember", because
`--show-config --json` does not report the checkpointer (ADR open question 4); the harness
relay uses Playwright's page bridge (`exposeFunction` + `postMessage`) instead of a WebSocket
server; reject / edit / Stop run against a small keyless fixture agent
(`extension/test/fixtures/hitl_agent.py`) because `--demo=tools` allows only respond and
approve and finishes too fast to stop; the recording is the harness page, not code-server;
`panel-e2e` runs on Ubuntu only.

### M5: tests and recording without Copilot (M, 3–5 d)
Three layers, all keyless, all driven by the real sidecar `--demo=tools` or recorded fixtures:

1. **Unit tests (vitest)** for `reducer.ts`, `panelProtocol`, `ConversationStore` and
   `buildDecisions` wiring. Fixtures are NDJSON transcripts captured from
   `python -m langstage_vscode --demo=tools`, committed under `extension/test/fixtures/`.
2. **Webview harness plus Playwright.** This is the fast, primary end-to-end layer.
   - `extension/test/harness/` serves `dist/webview/main.js` in a page that defines
     `acquireVsCodeApi()`.
   - That stub forwards messages over a WebSocket to `harness/relay.ts`, a small Node server
     that reuses **the real `SidecarClient` and `PanelController` logic** against a spawned
     `python -m langstage_vscode --demo=tools`. It binds 127.0.0.1 and is test-only; it never
     ships in the VSIX.
   - Playwright covers: streaming text, tool card, reasoning, todos, interrupt → Approve →
     resumed reply, Reject with reason, Edit JSON, Stop mid-turn, a second conversation
     queued, and startup failure (a bad agent spec).
   - `recordVideo` plus `trace: 'on'` produce a video artifact on every run.
3. **Real-editor layer.**
   - **`@vscode/test-electron`** (mocha, `xvfb-run` on Linux) asserts that the extension
     activates *without* `vscode.chat`, that the view resolves, and that
     `langstage.newConversation` plus a test-only command round-trip a `--demo` turn through
     the host.
   - **code-server recording** (Linux CI job; the `codercom/code-server` image or `npm i -g
     code-server`):
     - install the built VSIX, start with `--auth none --bind-addr 127.0.0.1:8080`, and open a
       fixture workspace whose `langstage.toml` points at the demo;
     - Playwright Chromium clicks the activity-bar icon and drives the panel through nested
       frames (`frameLocator('iframe.webview.ready').frameLocator('#active-frame')`);
     - `recordVideo` output is converted to the README GIF (ffmpeg) and uploaded as an
       artifact.

   code-server is also the proof that the panel runs in an OSS build with no Copilot, served
   from an Open VSX-style gallery.
- CI: extend `.github/workflows/ci.yml` `extension-build` with `npm run test:unit`, and add jobs
  `panel-e2e` (harness, ubuntu plus windows-latest for spawn and paths), `editor-smoke`
  (test-electron) and `panel-recording` (code-server, on `main` and on demand).

### M6: packaging, publishing, docs (S–M, 2–3 d)
- **VSIX:**
  - `.vscodeignore` ships only `dist/**`, `media/**`, `package.json`, README, LICENSE and
    CHANGELOG;
  - bundled with no `node_modules`;
  - CI fails if the VSIX exceeds 1.5 MB;
  - `engines.vscode` stays `^1.95.0`; check it against current Cursor and Windsurf bases
    before release;
  - no proposed APIs.
- **Publishing:** manual, matching the family's release process.
  - Marketplace: `vsce publish` with the `dkedar7` publisher (needs an Azure DevOps personal
    access token).
  - **Open VSX**: `npx ovsx publish <vsix> -p $OVSX_PAT` after the one-time `ovsx
    create-namespace dkedar7` and namespace-ownership claim.
  - Both use the same VSIX. Document the steps in `RELEASING.md`, or in the README if there is
    no releasing doc.
- **Manual smoke checklist per release:** VS Code without Copilot, Cursor, VSCodium, Windsurf,
  and code-server, on Windows and Linux. Install from the marketplace, open the panel, run
  `--demo=tools`, answer an interrupt, Stop, and reload the window.
- **Optional sidecar change:** `ready.version` and `ready.protocol` (sidecar 0.6.0, with a
  CHANGELOG entry and a test in `tests/test_sidecar.py`).
- **Docs:**
  - README: a new "The LangStage panel" section above "Answering an interrupt from the chat",
    with the recorded GIF;
  - Install: Marketplace or Open VSX, then `pip install langstage-vscode`, then set
    `langstage.agentSpec`;
  - reword "alongside Copilot" to "with or without Copilot";
  - the participant section becomes "For Copilot users";
  - `package.json` `description` and `displayName` are unchanged apart from dropping
    "alongside Copilot";
  - CHANGELOG entry for extension 0.5.0;
  - a langstage-docs page for the VS Code stage.

## Versioning

- Extension `0.4.0` → **`0.5.0`**: the panel, M0–M4, may ship as a preview once M3 lands, with the
  view contributed and the participant unchanged.
- The Python sidecar needs no release for the panel. It needs one only for the optional
  `ready.version` (0.6.0).

## Risks

| Risk | Mitigation |
|---|---|
| Webview state lost when hidden or reloaded | Host is the source of truth; `restore` on `ui/ready`; `retainContextWhenHidden` for the common case |
| One-turn-at-a-time sidecar feels blocking with several conversations | Queue shown explicitly; parallelism deferred (ADR open question 3) |
| Transcript shown but agent memory gone after reload | Detect the in-memory checkpointer; banner; docs point to durable checkpointers |
| Untrusted agent output (prompt-injected links or HTML) | No raw HTML; links through a host `openExternal` confirmation; strict CSP with no `connect-src` |
| Fork API drift (Cursor or Windsurf lag VS Code) | Stable APIs only; `vscode.chat` feature-detected; release smoke checklist |
| Duplicated UI with web and jupyter | Port with source pointers; revisit a shared `@langstage/chat-ui` package after M2 (ADR open question 1) |
| code-server end-to-end flakiness | The harness layer is the gate; the recording job is informative (non-blocking) until stable |
