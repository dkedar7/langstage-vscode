import * as vscode from 'vscode';

/** What both surfaces need to launch a sidecar. */
export interface LaunchConfig {
  python: string;
  agentSpec: string;
  workspace: string;
}

/** Read the launch settings (falling back to the legacy `deepagent.*` names). */
export function readLaunchConfig(): LaunchConfig {
  const config = vscode.workspace.getConfiguration('langstage');
  const legacy = vscode.workspace.getConfiguration('deepagent');
  const agentSpec = config.get<string>('agentSpec') || legacy.get<string>('agentSpec') || '';
  const python = config.get<string>('pythonPath') || legacy.get<string>('pythonPath') || 'python';
  const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
  return { python, agentSpec, workspace };
}

/** Whether a configuration change should restart a sidecar. */
export function affectsLaunch(e: vscode.ConfigurationChangeEvent): boolean {
  return e.affectsConfiguration('langstage') || e.affectsConfiguration('deepagent');
}
