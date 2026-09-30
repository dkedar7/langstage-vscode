/**
 * The outdated-sidecar warning (gh #89). The extension and the PyPI sidecar release
 * separately, and `langstage.pythonPath` can point at any installed langstage-vscode, so
 * a sidecar older than MIN_SIDECAR_VERSION (or one too old to say its version) gets a
 * warning with the command that upgrades it. It still runs: an older sidecar serves what
 * it can. (A sidecar with a newer protocol is refused instead, in `SidecarClient`.)
 *
 * The panel and `@langstage` spawn several sidecars over a session (restarts, new chats),
 * so the warning shows once per interpreter per activation, not once per process.
 *
 * No `vscode` import: the notification is injected (see notices.ts), so this runs under
 * `node --test`.
 */
import { MIN_SIDECAR_VERSION, SidecarInfo, sidecarCompatibility } from './sidecar';

/** The warning for one interpreter: its text, and the command the button copies. */
export interface OutdatedNotice {
  python: string;
  message: string;
  command: string;
}

/** `<python> -m pip install -U langstage-vscode`, quoting an interpreter path with spaces. */
export function pipUpgradeCommand(python: string): string {
  const exe = /\s/.test(python) ? `"${python}"` : python;
  return `${exe} -m pip install -U langstage-vscode`;
}

/** The warning for `python`'s sidecar, given the version it reported (if any). */
export function outdatedNotice(python: string, version: string | undefined): OutdatedNotice {
  const command = pipUpgradeCommand(python);
  const is = version ? `is ${version}` : `is older than ${MIN_SIDECAR_VERSION}`;
  return {
    python,
    command,
    message:
      `LangStage: the langstage-vscode sidecar in ${python} ${is}; this extension expects ` +
      `${MIN_SIDECAR_VERSION} or newer, so some features may not work. Update it with: ${command}`,
  };
}

/** Decides when to warn: once per interpreter, for a sidecar that is outdated. */
export class OutdatedSidecarWarnings {
  private readonly warned = new Set<string>();

  constructor(private readonly notify: (notice: OutdatedNotice) => void) {}

  /**
   * Warn about the sidecar `python` runs, if its handshake says it is outdated and this
   * interpreter has not been warned about yet. Returns whether it warned.
   */
  check(python: string, info: SidecarInfo): boolean {
    const compat = sidecarCompatibility(info);
    if (compat.kind !== 'outdated' || this.warned.has(python)) return false;
    this.warned.add(python);
    this.notify(outdatedNotice(python, compat.version));
    return true;
  }

  /** Forget who was warned (a new activation). */
  reset(): void {
    this.warned.clear();
  }
}
