/**
 * The webview's only channel to the extension host (ADR 0001): `acquireVsCodeApi()`.
 * Outside VS Code (the test harness) the page defines a stub with the same shape.
 */
import type { WebviewToHost } from '../src/shared/panelProtocol';

interface VsCodeApi {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}

declare function acquireVsCodeApi(): VsCodeApi;

const api: VsCodeApi = acquireVsCodeApi();

type Distribute<T> = T extends unknown ? Omit<T, 'v'> : never;

/** Send a protocol message to the host (the `v: 1` field is added here). */
export function post(message: Distribute<WebviewToHost>): void {
  api.postMessage({ v: 1, ...message });
}
