// Which Python runs the sidecar (gh #178): expand the variables people write in
// `langstage.pythonPath`, and find the workspace's venv when nothing is set. No vscode
// import, so `npm test` can exercise it; config.ts feeds it the settings and the folders.
import * as nodePath from 'path';

/** Where the interpreter came from, most specific first. */
export type PythonSource = 'setting' | 'legacy-setting' | 'env' | 'venv' | 'path';

export interface WorkspaceFolderRef {
  name: string;
  path: string;
}

export interface PythonInputs {
  /** `langstage.pythonPath` as someone set it (not the contributed default). */
  setting?: string;
  /** `deepagent.pythonPath`, the legacy name. */
  legacySetting?: string;
  env: Record<string, string | undefined>;
  /** The workspace folders in order; the first is the sidecar's working directory. */
  folders: WorkspaceFolderRef[];
  home: string;
  /** `process.platform`. */
  platform: string;
  exists(path: string): boolean;
}

export interface PythonChoice {
  /** What to spawn: an absolute path, or a bare command looked up on PATH. */
  python: string;
  source: PythonSource;
  /** How it was chosen, for the status line: `.venv (auto-detected)`, a setting's name... */
  how: string;
  /** The value can't be used (an unknown variable, no folder open): don't spawn. */
  error?: string;
}

/** The variables `langstage.pythonPath` understands, for the error message. */
export const SUPPORTED_VARIABLES =
  '${workspaceFolder}, ${workspaceFolder:<name>}, ${userHome}, ${env:<NAME>} and a leading ~';

/** Folder names tried, in order, when no interpreter is configured. */
export const VENV_DIRS = ['.venv', 'venv'];

function pathFor(platform: string): nodePath.PlatformPath {
  return platform === 'win32' ? nodePath.win32 : nodePath.posix;
}

/** A venv's interpreter, relative to the venv's folder. */
export function venvInterpreter(platform: string): string[] {
  return platform === 'win32' ? ['Scripts', 'python.exe'] : ['bin', 'python'];
}

type Expanded = { ok: true; value: string; changed: boolean } | { ok: false; error: string };

/**
 * Substitute `${workspaceFolder}`, `${workspaceFolder:<name>}`, `${userHome}`,
 * `${env:<NAME>}` and a leading `~`: the subset of VS Code's launch.json variables that
 * makes sense for a path. Anything else in `${...}` is refused by name, so a typo doesn't
 * end as `spawn ENOENT`.
 */
export function expandVariables(raw: string, inputs: PythonInputs, name: string): Expanded {
  const p = pathFor(inputs.platform);
  const errors: string[] = [];
  let changed = false;
  let value = raw.replace(/\$\{([^}]*)\}/g, (whole, inner: string) => {
    changed = true;
    if (inner === 'workspaceFolder') {
      const first = inputs.folders[0];
      if (!first) errors.push(`${name} uses \${workspaceFolder}, but no folder is open.`);
      return first?.path ?? whole;
    }
    if (inner.startsWith('workspaceFolder:')) {
      const wanted = inner.slice('workspaceFolder:'.length);
      const folder = inputs.folders.find((f) => f.name === wanted);
      if (!folder) {
        const open = inputs.folders.map((f) => f.name).join(', ') || 'none';
        errors.push(`${name} uses \${${inner}}, but no workspace folder is named "${wanted}" (open: ${open}).`);
      }
      return folder?.path ?? whole;
    }
    if (inner === 'userHome') return inputs.home;
    if (inner.startsWith('env:')) return inputs.env[inner.slice('env:'.length)] ?? '';
    errors.push(`${name} uses \${${inner}}, which isn't supported here. Supported: ${SUPPORTED_VARIABLES}.`);
    return whole;
  });
  if (errors.length) return { ok: false, error: errors[0] };
  if (value === '~' || /^~[\\/]/.test(value)) {
    value = p.join(inputs.home, value.slice(1));
    changed = true;
  }
  return { ok: true, value, changed };
}

/**
 * A configured value as the thing to spawn. An absolute path with nothing to expand and a
 * bare command (`python`, `py`) are returned untouched. A relative path is resolved
 * against the first workspace folder on every OS. A venv's folder stands for its
 * interpreter, so one committed value works on Windows and POSIX alike.
 */
function fromConfigured(raw: string, source: PythonSource, how: string, inputs: PythonInputs): PythonChoice {
  const p = pathFor(inputs.platform);
  const expanded = expandVariables(raw, inputs, how);
  if (!expanded.ok) return { python: raw, source, how, error: expanded.error };
  let python = expanded.value;
  if (!python) {
    return { python: raw, source, how, error: `${how} is "${raw}", which expands to nothing.` };
  }
  const isPath = /[\\/]/.test(python);
  if (isPath && !p.isAbsolute(python)) {
    const base = inputs.folders[0]?.path;
    if (base) python = p.resolve(base, python);
  } else if (isPath && expanded.changed) {
    python = p.normalize(python);
  }
  if (isPath) {
    const inVenv = p.join(python, ...venvInterpreter(inputs.platform));
    if (inputs.exists(inVenv)) python = inVenv;
  }
  return { python, source, how };
}

/**
 * Pick the interpreter: `langstage.pythonPath`, else the legacy `deepagent.pythonPath`,
 * else `$LANGSTAGE_PYTHON`, else the first workspace folder's `.venv` or `venv` if it
 * has an interpreter, else `python` on PATH.
 */
export function resolvePython(inputs: PythonInputs): PythonChoice {
  if (inputs.setting) return fromConfigured(inputs.setting, 'setting', 'langstage.pythonPath', inputs);
  if (inputs.legacySetting) {
    return fromConfigured(inputs.legacySetting, 'legacy-setting', 'deepagent.pythonPath', inputs);
  }
  const fromEnv = inputs.env.LANGSTAGE_PYTHON;
  if (fromEnv) return fromConfigured(fromEnv, 'env', 'LANGSTAGE_PYTHON', inputs);

  const p = pathFor(inputs.platform);
  const first = inputs.folders[0];
  if (first) {
    for (const dir of VENV_DIRS) {
      const candidate = p.join(first.path, dir, ...venvInterpreter(inputs.platform));
      if (inputs.exists(candidate)) {
        return { python: candidate, source: 'venv', how: `${dir} (auto-detected)` };
      }
    }
  }
  return { python: 'python', source: 'path', how: 'python on PATH' };
}

/**
 * What to say when the interpreter can't be started at all (`spawn ... ENOENT`): which
 * one, where the value came from, and the two ways out.
 */
export function interpreterNotFoundMessage(python: string, how: string | undefined): string {
  const from = how ? ` (from ${how})` : '';
  return (
    `Python interpreter not found: ${python}${from}. Set langstage.pythonPath to an ` +
    `interpreter that has langstage-vscode installed (for example ` +
    `\${workspaceFolder}/.venv), or leave it empty to use the workspace's .venv.`
  );
}
