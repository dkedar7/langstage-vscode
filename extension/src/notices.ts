import * as vscode from 'vscode';
import { OutdatedNotice, OutdatedSidecarWarnings } from './outdatedSidecar';

/** The outdated-sidecar notification's button. */
export const COPY_COMMAND = 'Copy command';

/**
 * Show the outdated-sidecar warning (gh #89) as a VS Code warning notification. Its
 * "Copy command" button puts the upgrade command on the clipboard.
 */
export function showOutdatedSidecarNotice(notice: OutdatedNotice): void {
  void vscode.window.showWarningMessage(notice.message, COPY_COMMAND).then((choice) => {
    if (choice === COPY_COMMAND) void vscode.env.clipboard.writeText(notice.command);
  });
}

/**
 * Shared by the panel and `@langstage`, so each interpreter is warned about once, however
 * many sidecars the two spawn. `activate()` resets it, so it is once per activation.
 */
export const outdatedSidecarWarnings = new OutdatedSidecarWarnings(showOutdatedSidecarNotice);
