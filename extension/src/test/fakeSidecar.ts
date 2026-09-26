// A fake `python -m langstage_vscode` child process for SidecarClient tests: it replays
// NDJSON frames on stdout and records the commands written to stdin.
import { EventEmitter } from 'events';
import * as fs from 'fs';
import * as path from 'path';
import { PassThrough } from 'stream';
import type { ChildProcess } from 'child_process';
import type { SidecarFrame, SpawnFn } from '../sidecar';

export class FakeProc extends EventEmitter {
  stdout = new PassThrough();
  stderr = new PassThrough();
  stdin = new PassThrough();
  commands: Array<Record<string, unknown>> = [];
  killed = false;
  private buf = '';

  constructor() {
    super();
    this.stdin.on('data', (chunk: Buffer) => {
      this.buf += chunk.toString();
      let nl: number;
      while ((nl = this.buf.indexOf('\n')) >= 0) {
        const line = this.buf.slice(0, nl);
        this.buf = this.buf.slice(nl + 1);
        if (line.trim()) {
          const cmd = JSON.parse(line) as Record<string, unknown>;
          this.commands.push(cmd);
          this.emit('command', cmd);
        }
      }
    });
  }

  emitFrames(frames: SidecarFrame[]): void {
    for (const f of frames) this.stdout.write(JSON.stringify(f) + '\n');
  }

  exit(code: number): void {
    this.stdout.end();
    this.emit('exit', code);
    setImmediate(() => this.emit('close', code));
  }

  kill(): boolean {
    this.killed = true;
    this.exit(0);
    return true;
  }
}

export function fakeSpawn(): { spawn: SpawnFn; procs: FakeProc[] } {
  const procs: FakeProc[] = [];
  const spawn: SpawnFn = () => {
    const p = new FakeProc();
    procs.push(p);
    return p as unknown as ChildProcess;
  };
  return { spawn, procs };
}

/** The recorded `--demo=tools` transcript, split into turns (each ends on turn_end). */
export function demoToolsTurns(): SidecarFrame[][] {
  const file = path.resolve('src/test/fixtures/demo-tools.ndjson');
  const frames = fs
    .readFileSync(file, 'utf8')
    .split(/\r?\n/)
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l) as SidecarFrame)
    .filter((f) => f.type !== 'ready');
  const turns: SidecarFrame[][] = [];
  let cur: SidecarFrame[] = [];
  for (const f of frames) {
    cur.push(f);
    if (f.type === 'turn_end') {
      turns.push(cur);
      cur = [];
    }
  }
  return turns;
}

export const tick = () => new Promise((r) => setImmediate(r));
