import { spawn } from 'node:child_process';
import type { ChildProcessWithoutNullStreams } from 'node:child_process';
import { Failure } from './errors.js';
import type { ProcessEnv } from './types.js';

export interface ChildResult { code: number; stdout: string; stderr?: string }
export interface ManagedChild {
  process: ChildProcessWithoutNullStreams;
  done: Promise<ChildResult>;
  stop(): Promise<void>;
}
// Neither stderr nor spawn errors are forwarded: both may contain credentials.
export function startChild(program: string, args: string[], env: ProcessEnv, cwd: string,
  timeout: number, signal?: AbortSignal, onOutput?: (text: string) => void, captureStderr = false): ManagedChild {
  const child = spawn(program, args, { env, cwd, detached: true, stdio: 'pipe' });
  let output = '', errorOutput = '', bytes = 0, failure: Failure | undefined;
  const kill = (name: NodeJS.Signals): void => {
    if (child.pid) { try { process.kill(-child.pid, name); } catch { /* already gone */ } }
  };
  let escalation: ReturnType<typeof setTimeout> | undefined;
  const abort = (): void => {
    failure ??= new Failure('tool_failed', 'The operation was stopped.'); kill('SIGTERM');
    escalation ??= setTimeout(() => kill('SIGKILL'), 500);
  };
  const timer = setTimeout(() => { failure = new Failure('tool_failed', 'The child command timed out.'); kill('SIGKILL'); }, timeout);
  const done = new Promise<ChildResult>((resolve, reject) => {
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (text: string) => {
      bytes += Buffer.byteLength(text);
      if (bytes > 2 * 1024 * 1024) { failure = new Failure('tool_failed', 'The child command returned too much output.'); kill('SIGKILL'); return; }
      output += text; onOutput?.(output);
    });
    if (captureStderr) {
      child.stderr.setEncoding('utf8'); child.stderr.on('data', (text: string) => {
        bytes += Buffer.byteLength(text);
        if (bytes > 2 * 1024 * 1024) { failure = new Failure('tool_failed', 'The child command returned too much output.'); kill('SIGKILL'); return; }
        errorOutput += text;
      });
    } else child.stderr.resume();
    child.stdin.on('error', () => {});
    child.on('error', () => { failure = new Failure('tool_failed', 'The child command could not start.'); });
    child.on('exit', () => kill('SIGKILL'));
    child.on('close', (code) => {
      clearTimeout(timer); clearTimeout(escalation); signal?.removeEventListener('abort', abort);
      kill('SIGKILL'); // end descendants even when the command itself has exited
      if (failure) reject(failure); else resolve({ code: code ?? 1, stdout: output, ...(captureStderr ? { stderr: errorOutput } : {}) });
    });
  });
  // A caller may be preparing its input before it awaits completion.
  void done.catch(() => {});
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) abort();
  return { process: child, done, async stop() {
    kill('SIGTERM');
    const escalation = setTimeout(() => kill('SIGKILL'), 500);
    try { await done.catch(() => {}); } finally { clearTimeout(escalation); kill('SIGKILL'); }
  } };
}
export async function childCommand(program: string, args: string[], env: ProcessEnv, cwd: string,
  timeout = 20000, signal?: AbortSignal, input?: string, captureStderr = false): Promise<ChildResult> {
  const child = startChild(program, args, env, cwd, timeout, signal, undefined, captureStderr);
  child.process.stdin.end(input);
  try { return await child.done; } finally { await child.stop(); }
}
