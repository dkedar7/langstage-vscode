import * as vscode from 'vscode';
import { withLegacyFallback } from './launchSettings';

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
  // Only values someone set: get() returns the contributed default ("python"), which
  // would always beat a legacy deepagent.pythonPath (gh #168).
  const agentSpec = withLegacyFallback<string>(config.inspect('agentSpec'), legacy.inspect('agentSpec'), '');
  const python = withLegacyFallback<string>(config.inspect('pythonPath'), legacy.inspect('pythonPath'), 'python');
  const workspace = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? process.cwd();
  return { python, agentSpec, workspace };
}

/** Whether a configuration change should restart a sidecar. */
export function affectsLaunch(e: vscode.ConfigurationChangeEvent): boolean {
  return e.affectsConfiguration('langstage') || e.affectsConfiguration('deepagent');
}
