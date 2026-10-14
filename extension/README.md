# LangStage for VS Code

Chat with your own **LangGraph** agent from inside your editor. The **LangStage panel** is
the extension's own chat view, so it works in VS Code **with or without Copilot**, and in
Cursor, VSCodium, Windsurf and code-server.

<p align="center">
  <img src="https://dkedar7.github.io/langstage-docs/assets/demos/vscode.gif" alt="The LangStage panel running the keyless demo agent: a tool card, reasoning, an approval prompt answered with Approve, and the resumed reply" width="440">
</p>

## Install: two parts

The extension runs your agent through a small Python process, the **sidecar**, which comes
from PyPI. Installing the extension alone is not enough: you need both parts.

1. **Install the sidecar** into the Python environment that can import your agent:

   ```bash
   pip install langstage-vscode
   ```

2. **Install this extension** from the Extensions view: search for **LangStage**. Cursor,
   VSCodium and Windsurf install it from
   [Open VSX](https://open-vsx.org/extension/dkedar7/langstage-vscode).

3. **Point `langstage.pythonPath` at that interpreter** if it isn't the `python` on your
   `PATH`, for example `/home/me/project/.venv/bin/python` or
   `C:\Users\me\project\.venv\Scripts\python.exe`.

If the interpreter doesn't have the sidecar, the panel says so and gives you the
`<python> -m pip install langstage-vscode` command for it. The extension expects
langstage-vscode **0.5.35 or newer**. It still runs an older one, but shows a warning with
the upgrade command.

## Try the demo

Open the **LangStage** view in the activity bar. With no agent configured, the panel says
*No agent configured* and offers **Try the demo**: a keyless demo agent (`--demo=tools`)
for that session, with no API key needed. Include one of these phrases in your message to
see each kind of output:

- `use a tool` shows a tool-call card;
- `think` shows a reasoning block;
- `ask me` shows an approval card.

Any other message is echoed back.

## Run your agent

Set **`langstage.agentSpec`** to your agent: `path/to/agent.py:graph` or
`package.module:graph`. A relative path resolves against the workspace folder. The agent
is any LangGraph `CompiledGraph`, for example from `deepagents`:

```python
# my_agent.py  ->  "langstage.agentSpec": "my_agent.py:graph"
from deepagents import create_deep_agent

graph = create_deep_agent(...)
```

Leave `langstage.agentSpec` empty to use `LANGSTAGE_AGENT_SPEC` or the project's
`langstage.toml` (`[agent] spec = "my_agent.py:graph"`), the same config every LangStage
stage reads.

### Workspace root

The agent runs in your workspace folder (the first one, in a multi-root workspace) unless
you've set a root yourself: a `[workspace] root` in `langstage.toml` (a relative path is
taken from that file's folder) or a `LANGSTAGE_WORKSPACE_ROOT` in the environment VS Code
was started with. Those win in the same order as everywhere else (`langstage.toml`, then
the environment), so `langstage-vscode-sidecar --show-config`, run from the workspace
folder, shows the root the panel and `@langstage` use.

## Settings

| Setting | What it does | Default |
|---|---|---|
| `langstage.agentSpec` | Your agent, as `path/to/agent.py:graph` or `module:graph`. Empty falls back to `LANGSTAGE_AGENT_SPEC` or `langstage.toml`. | _(empty)_ |
| `langstage.pythonPath` | The Python interpreter that runs the sidecar. It must have `langstage-vscode` installed. | `python` |

The old `deepagent.agentSpec` and `deepagent.pythonPath` settings are still read as a
fallback.

## Commands

| Command | What it does |
|---|---|
| **LangStage: Open the LangStage panel** | Opens the chat panel. |
| **LangStage: New conversation** | Starts a new conversation (also the **+** in the panel's title bar). |
| **LangStage: Restart the agent** | Restarts the sidecar, for example after you edit your agent. |

## What you get

- Replies stream in as markdown.
- Tool calls appear as collapsible cards with the arguments, the result, the status and
  the duration. Reasoning appears in a collapsed block. A `write_todos` plan shows as a
  live **Tasks** checklist.
- **Approval cards** for human-in-the-loop interrupts, with **Approve**, **Reject** (with
  an optional reason), **Respond**, and **Edit** (the arguments as JSON).
- **Stop** cancels a turn, and the agent still remembers the conversation.
- **Several conversations per workspace**, each with its own memory. Transcripts are saved
  on this machine and come back after a window reload.
- If you use Copilot, the same agent is also available as the **`@langstage`** chat
  participant, with `/approve`, `/reject`, `/respond` and `/edit`.

Agent output is untrusted: the panel never renders raw HTML, and a link opens only after
you confirm it. The extension is disabled in untrusted (Restricted Mode) workspaces,
because running your agent executes workspace code.

## Troubleshooting

If the panel says **The agent failed to start**, it shows the sidecar's startup error, and
the full stderr is one click away. To check the interpreter and your agent outside the
editor, use the same interpreter as `langstage.pythonPath`:

```bash
python -m langstage_vscode --selfcheck --agent ./my_agent.py:graph
```

## Learn more

- [Documentation](https://dkedar7.github.io/langstage-docs) for the LangStage family
- [Full README](https://github.com/dkedar7/langstage-vscode#readme): configuration, the
  sidecar's CLI and its stdio protocol
- [The sidecar on PyPI](https://pypi.org/project/langstage-vscode/)
- [Report an issue](https://github.com/dkedar7/langstage-vscode/issues)

LangStage runs the same agent on every stage: a
[web app](https://github.com/dkedar7/langstage),
[JupyterLab](https://github.com/dkedar7/langstage-jupyter), the
[terminal](https://github.com/dkedar7/langstage-cli) and VS Code. All of them share the
same spec string, `langstage.toml` and `LANGSTAGE_*` variables.
