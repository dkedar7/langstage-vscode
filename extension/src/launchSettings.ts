// Choosing between a `langstage.*` setting and its legacy `deepagent.*` name. No vscode
// import, so `npm test` can exercise it; config.ts feeds it `WorkspaceConfiguration.inspect()`.

/** `WorkspaceConfiguration.inspect()`: the default plus the layers someone can set. */
export interface SettingLayers<T> {
  key?: string;
  defaultValue?: T;
  globalValue?: T;
  workspaceValue?: T;
  workspaceFolderValue?: T;
}

/** The value set at the most specific layer (folder, workspace, user), if any. */
export function explicitValue<T>(layers: SettingLayers<T> | undefined): T | undefined {
  if (!layers) {
    return undefined;
  }
  return layers.workspaceFolderValue ?? layers.workspaceValue ?? layers.globalValue;
}

/**
 * A `langstage.*` setting, else its legacy `deepagent.*` name, else the default.
 *
 * Only values someone actually set count. `get()` would return the contributed default
 * (`langstage.pythonPath` defaults to "python"), which always beat the legacy setting
 * (gh #168). An empty string counts as unset, as before.
 */
export function withLegacyFallback<T>(
  current: SettingLayers<T> | undefined,
  legacy: SettingLayers<T> | undefined,
  fallback: T,
): T {
  return explicitValue(current) || explicitValue(legacy) || fallback;
}
