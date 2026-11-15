import * as fs from 'fs';
import * as os from 'os';
import * as vscode from 'vscode';
import { explicitValue, withLegacyFallback } from './launchSettings';
import { PythonChoice, resolvePython } from './pythonPath';

/** What both surfaces need to launch a sidecar. */
export interface LaunchConfig {
  /** The interpreter to spawn: `interpreter.python`. */
  python: string;
  /** How the interpreter was chosen (gh #178), and why it can't be used, if so. */
  interpreter: PythonChoice;
  agentSpec: string;
  workspace: string;
}

/** Read the launch settings (falling back to the legacy `deepagent.*` names). */
export function readLaunchConfig(): LaunchConfig {
  const config = vscode.workspace.getConfiguration('langstage');
  const legacy = vscode.workspace.getConfiguration('deepagent');
  // Only values someone set: get() returns the contributed default, which would always
  // beat a legacy deepagent.pythonPath (gh #168) and the workspace's venv (gh #178).
  const agentSpec = withLegacyFallback<string>(config.inspect('agentSpec'), legacy.inspect('agentSpec'), '');
  const folders = vscode.workspace.workspaceFolders ?? [];
  const interpreter = resolvePython({
    setting: explicitValue<string>(config.inspect('pythonPath')),
    legacySetting: explicitValue<string>(legacy.inspect('pythonPath')),
    env: process.env,
    folders: folders.map((f) => ({ name: f.name, path: f.uri.fsPath })),
    home: os.homedir(),
    platform: process.platform,
    exists: (p) => fs.existsSync(p),
  });
  const workspace = folders[0]?.uri.fsPath ?? process.cwd();
  return { python: interpreter.python, interpreter, agentSpec, workspace };
}

/** Whether a configuration change should restart a sidecar. */
export function affectsLaunch(e: vscode.ConfigurationChangeEvent): boolean {
  return e.affectsConfiguration('langstage') || e.affectsConfiguration('deepagent');
}
