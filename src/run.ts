import { spawn } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { createInterface } from 'node:readline';
import { rm } from 'node:fs/promises';
import { join, delimiter } from 'node:path';
import { Failure, safeText } from './errors.js';
import { isObject } from './settings.js';
import { fixedWait, privateDirectory, processOwner } from './state.js';
import { inspectTool, requireTool } from './tool.js';
import { Secrets, keychainService, validateKey } from './secrets.js';
import { Runs } from './runs.js';
import { readRequest, runArguments, userRecord } from './run-request.js';
import { RunProtocol } from './run-protocol.js';
import type { RunEvents } from './run-events.js';
import type { Subscriptions } from './subscriptions.js';
import type { Command } from './arguments.js';
import type { ProcessEnv } from './types.js';
import { userInfo } from 'node:os';

export async function run(command: Command, subscriptions: Subscriptions, events: RunEvents,
  env: ProcessEnv, build: string, signal: AbortSignal): Promise<number> {
  const input = command.options.request === '-' || command.options.control ? createInterface({ input: process.stdin, crlfDelay: Infinity }) : undefined;
  try {
  const lines = input?.[Symbol.asyncIterator]();
  const request = await readRequest(command, subscriptions.settings, async () => {
    const line = await lines!.next(); if (line.done) throw new Failure('invalid_input', 'The request line is missing.'); return line.value;
  }, (runId, labels) => { events.runId = runId; events.labels = labels; }).catch(error => { input?.close(); throw error; });
  events.runId = request.runId; events.labels = request.labels;
  if (!command.options.control) input?.close();
  const subscription = await subscriptions.requireSignedIn(request.subscriptionId);
  const tool = await inspectTool(subscriptions.state, subscriptions.settings.toolPath, env, signal); requireTool(tool);
  const secrets: string[] = [];
  let key: string | undefined;
  if (subscription.family === 'zai') {
    const source = new Secrets(await keychainService(subscriptions.state.directory), userInfo().homedir, env, signal);
    key = await source.read(subscription.id, subscriptions.settings.subscriptions?.[subscription.id]?.secret);
    if (!key) throw new Failure('credential_rejected', 'The Subscription has no key.', { remedy: 'Replace the key.' });
    validateKey(key); secrets.push(key);
  }
  const runs = new Runs(subscriptions.state);
  const conversation = randomUUID();
  const record = await runs.reserve(request, conversation);
  const temp = join(subscriptions.state.directory, 'tmp', request.runId);
  let error: unknown, cancelled = false;
  try {
    signal.throwIfAborted();
    await privateDirectory(temp);
    const cwd = request.workingDirectory ?? join(temp, 'work');
    if (request.workingDirectory === undefined) await privateDirectory(cwd);
    const childEnv = await subscriptions.environment(subscription.id, env, key);
    childEnv.CLAUDE_CODE_DISABLE_AUTO_MEMORY = '1'; childEnv.BASH_DEFAULT_TIMEOUT_MS = '300000'; childEnv.BASH_MAX_TIMEOUT_MS = '1800000';
    if (request.searchPath.length) childEnv.PATH = (childEnv.PATH ?? '') + delimiter + request.searchPath.join(delimiter);
    const child = spawn(tool.path!, runArguments(request, conversation, command.options.control === true), { env: childEnv, cwd, detached: true, stdio: 'pipe' });
    let stderr = '', spawnError = false;
    const kill = (name: NodeJS.Signals): void => { if (child.pid) { try { process.kill(-child.pid, name); } catch { /* gone */ } } };
    const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
      child.on('error', () => { spawnError = true; }); child.on('close', (code, signal) => resolve({ code, signal }));
    });
    child.stdin.on('error', () => {});
    child.stderr.setEncoding('utf8'); child.stderr.on('data', (text: string) => { stderr = safeText(stderr + text, secrets, 4096); });
    const send = async (record: object): Promise<void> => {
      await new Promise<void>((resolve, reject) => child.stdin.write(JSON.stringify(record) + '\n', error => error ? reject(new Failure('tool_failed', 'Claude input closed.')) : resolve()));
    };
    const protocol = new RunProtocol(events, request, build, tool.version!, subscription.family, send);
    let stopTimer: ReturnType<typeof setTimeout> | undefined;
    const stop = (): void => {
      cancelled ||= signal.aborted;
      void send({ type: 'control_request', request_id: randomUUID(), request: { subtype: 'interrupt' } }).catch(() => {});
      stopTimer ??= setTimeout(() => kill('SIGKILL'), fixedWait(2000));
    };
    signal.addEventListener('abort', stop, { once: true });
    const outputError = (): void => { cancelled = true; stop(); };
    events.output.on('error', outputError);
    const startTimer = setTimeout(() => { error = new Failure('limit_reached', 'Claude did not start within sixty seconds.', { detail: { basis: 'fields', limit: 'start' } }); kill('SIGKILL'); }, fixedWait(60000));
    let endTimer: ReturnType<typeof setTimeout> | undefined;
    const stdout = createInterface({ input: child.stdout, crlfDelay: Infinity });
    let gotSettings = false, initialized = false, promptSent = false;
    const settingsId = randomUUID(), initializeId = randomUUID();
    const controlTask = command.options.control ? (async () => {
      for await (const line of { [Symbol.asyncIterator]: () => lines! }) {
        try {
          const value: unknown = JSON.parse(line);
          if (isObject(value) && value.type === 'stop') { cancelled = true; stop(); break; }
          await events.emit({ type: 'control_refused', error: new Failure('invalid_input', 'This control line is not accepted.', {}, secrets).result,
            ...(isObject(value) && typeof value.askId === 'string' ? { askId: value.askId } : {}) });
        } catch {
          await events.emit({ type: 'control_refused', error: new Failure('invalid_input', 'The control line must be JSON.').result });
        }
      }
      if (!protocol.result && !events.finished) { cancelled = true; stop(); }
    })().catch(() => { cancelled = true; stop(); }) : undefined;
    try {
      if (child.pid) { const owner = await processOwner(child.pid); if (owner) { record.tool = owner; await runs.save(record); } }
      if (signal.aborted) stop();
      await send({ type: 'control_request', request_id: initializeId, request: { subtype: 'initialize' } });
      for await (const line of stdout) {
        let value: unknown;
        try { value = JSON.parse(line); } catch { throw new Failure('tool_unsupported', 'Claude returned invalid JSON.'); }
        if (isObject(value) && value.type === 'control_response' && isObject(value.response)) {
          const response = value.response as Record<string, any>;
          if (response.request_id === initializeId) {
            if (initialized || response.subtype !== 'success' || !isObject(response.response) || !Array.isArray(response.response.models)) throw new Failure('tool_unsupported', 'Claude could not report its model catalog.');
            initialized = true;
            const models = response.response.models;
            const model = models.find((item: any) => item.value === request.model || item.resolvedModel === request.model);
            if (subscription.family === 'anthropic' && request.model && !model) throw new Failure('invalid_input', 'The requested model is not in the tool catalog.');
            if (subscription.family === 'anthropic' && request.effort && model && !(model.supportedEffortLevels ?? []).includes(request.effort)) throw new Failure('invalid_input', 'The requested effort is not supported by the model.');
            await send({ type: 'control_request', request_id: settingsId, request: { subtype: 'get_settings' } });
          } else if (response.request_id === settingsId) {
            if (gotSettings || response.subtype !== 'success' || !isObject(response.response) || !isObject(response.response.applied)) throw new Failure('tool_unsupported', 'Claude could not report its applied settings.');
            gotSettings = true;
            if (request.effort && response.response.applied.effort !== request.effort) throw new Failure('invalid_input', 'Claude did not apply the requested effort.');
            if (typeof response.response.applied.effort === 'string') request.effort = response.response.applied.effort;
            await send(userRecord(request)); promptSent = true;
          }
        } else await protocol.accept(value);
        if (protocol.started) clearTimeout(startTimer);
        if (protocol.result && (!protocol.background || protocol.error)) {
          child.stdin.end(); endTimer = setTimeout(() => kill('SIGKILL'), fixedWait(2000));
          break;
        }
      }
      const exit = await closed;
      if (spawnError) throw new Failure('tool_failed', 'Claude could not start.');
      if (protocol.error) throw protocol.error;
      if (!protocol.result && !cancelled) throw new Failure('tool_failed', `Claude ended without a result${stderr ? ': ' + stderr : '.'}`, {
        detail: { basis: 'fields', ...(exit.code === null ? {} : { toolExit: exit.code }), ...(stderr ? { excerpt: stderr } : {}) } }, secrets);
      if (!promptSent && !cancelled) throw new Failure('tool_unsupported', 'Claude did not complete its settings handshake.');
    } catch (failure) { error ??= failure; kill('SIGKILL'); await closed; }
    finally {
      clearTimeout(startTimer); clearTimeout(stopTimer); clearTimeout(endTimer);
      signal.removeEventListener('abort', stop); events.output.off('error', outputError);
      stdout.close(); input?.close(); if (controlTask) await controlTask;
      kill('SIGKILL');
    }
  } catch (failure) { error ??= failure; cancelled ||= signal.aborted; }
  finally { input?.close(); await rm(temp, { recursive: true, force: true }).catch(failure => { error ??= failure; }); }
  try { return await events.finish(error, cancelled); }
  finally { await runs.remove(request.runId); }
  } finally { input?.close(); }
}
