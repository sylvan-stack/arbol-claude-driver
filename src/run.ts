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
import { endRunGroups, runLaunchers } from './process-groups.js';
import { Chats } from './continuity.js';
import { MessageDigest, textHistory } from './history.js';
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
  const chats = new Chats(subscriptions.state);
  const home = join(subscriptions.state.subscription(subscription.id), 'home');
  const { plan, record } = await chats.reserve(request, runs, home);
  const fingerprint = new MessageDigest();
  for (const item of request.history) fingerprint.add(item);
  fingerprint.add({ type: 'message', ...request.input });
  events.onText = text => fingerprint.add({ type: 'message', role: 'assistant', text });
  const temp = join(subscriptions.state.directory, 'tmp', request.runId);
  let error: unknown, cancelled = false, toolSettled = false;
  let stopActive: (() => void) | undefined;
  const controlTask = command.options.control ? (async () => {
    for await (const line of { [Symbol.asyncIterator]: () => lines! }) {
      try {
        const value: unknown = JSON.parse(line);
        if (isObject(value) && value.type === 'stop') { cancelled = true; stopActive?.(); break; }
        await events.emit({ type: 'control_refused', error: new Failure('invalid_input', 'This control line is not accepted.', {}, secrets).result,
          ...(isObject(value) && typeof value.askId === 'string' ? { askId: value.askId } : {}) });
      } catch {
        await events.emit({ type: 'control_refused', error: new Failure('invalid_input', 'The control line must be JSON.').result });
      }
    }
    if (!toolSettled) { cancelled = true; stopActive?.(); }
  })().catch(() => { if (!toolSettled) { cancelled = true; stopActive?.(); } }) : undefined;
  try {
    signal.throwIfAborted();
    await privateDirectory(temp);
    const cwd = request.workingDirectory ?? join(temp, 'work');
    if (request.workingDirectory === undefined) await privateDirectory(cwd);
    const childEnv = await subscriptions.environment(subscription.id, env, key);
    const launchers = await runLaunchers(temp, env);
    childEnv.CLAUDE_CODE_SHELL = launchers.shell;
    childEnv.CLAUDE_CODE_DISABLE_AUTO_MEMORY = '1'; childEnv.BASH_DEFAULT_TIMEOUT_MS = '300000'; childEnv.BASH_MAX_TIMEOUT_MS = '1800000';
    if (request.searchPath.length) childEnv.PATH = (childEnv.PATH ?? '') + delimiter + request.searchPath.join(delimiter);
    await chats.prepare(plan, request, home, cwd);
    for (let attempt = 0; attempt < 2; attempt++) {
      const child = spawn(launchers.launcher, [tool.path!, ...runArguments(request, plan.conversation, command.options.control === true, plan.resume)], { env: childEnv, cwd, detached: true, stdio: 'pipe' });
      let stderr = '', spawnError = false;
      let stopDeadline: number | undefined;
      let cleanup: Promise<number[]> | undefined;
      const kill = (name: NodeJS.Signals): void => { if (child.pid) { try { process.kill(-child.pid, name); } catch { /* gone */ } } };
      const closed = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
        child.on('error', () => { spawnError = true; }); child.on('close', (code, signal) => resolve({ code, signal }));
      });
      let exhausted: (() => void) | undefined;
      const forcedClose = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(resolve => {
        exhausted = () => resolve({ code: null, signal: 'SIGKILL' });
      });
      const boundedClose = (): Promise<{ code: number | null; signal: NodeJS.Signals | null }> => Promise.race([closed, forcedClose]);
      child.once('exit', () => {
        kill('SIGKILL');
        cleanup = endRunGroups(temp, record.tool, stopDeadline);
        void cleanup.catch(() => {});
      });
      child.stdin.on('error', () => {});
      child.stderr.setEncoding('utf8'); child.stderr.on('data', (text: string) => { stderr = safeText(stderr + text, secrets, 4096); });
      const send = async (record: object): Promise<void> => {
        await new Promise<void>((resolve, reject) => child.stdin.write(JSON.stringify(record) + '\n', error => error ? reject(new Failure('tool_failed', 'Claude input closed.')) : resolve()));
      };
      const protocol = new RunProtocol(events, request, build, tool.version!, subscription.family, send, plan, command.options.control === true);
      let stopTimer: ReturnType<typeof setTimeout> | undefined;
      let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
      let interruptId: string | undefined;
      const stop = (): void => {
        cancelled ||= signal.aborted;
        if (interruptId) return;
        interruptId = randomUUID(); stopDeadline = performance.now() + fixedWait(15000);
        void send({ type: 'control_request', request_id: interruptId, request: { subtype: 'interrupt' } }).catch(() => {});
        stopTimer = setTimeout(() => kill('SIGKILL'), fixedWait(2000));
        deadlineTimer = setTimeout(() => { kill('SIGKILL'); child.stdin.destroy(); child.stdout.destroy(); child.stderr.destroy(); exhausted?.(); }, fixedWait(15000));
      };
      stopActive = stop;
      signal.addEventListener('abort', stop, { once: true });
      const outputError = (): void => { cancelled = true; stop(); };
      events.output.on('error', outputError);
      const startTimer = setTimeout(() => { error = new Failure('limit_reached', 'Claude did not start within sixty seconds.', { detail: { basis: 'fields', limit: 'start' } }); kill('SIGKILL'); }, fixedWait(60000));
      let endTimer: ReturnType<typeof setTimeout> | undefined;
      const stdout = createInterface({ input: child.stdout, crlfDelay: Infinity });
      let gotSettings = false, initialized = false, promptSent = false;
      const settingsId = randomUUID(), initializeId = randomUUID();
      let resumeRefused = false;
      let pendingInit: unknown;
      const isRefusal = (text: string): boolean => /no conversation (?:was )?found|conversation.*not found|session.*not found/i.test(text);
      try {
        if (child.pid) { const owner = await processOwner(child.pid); if (owner) { record.tool = owner; await runs.save(record); } }
        if (signal.aborted || cancelled) stop();
        await send({ type: 'control_request', request_id: initializeId, request: { subtype: 'initialize' } });
        for await (const line of stdout) {
          let value: unknown;
          try { value = JSON.parse(line); } catch { throw new Failure('tool_unsupported', 'Claude returned invalid JSON.'); }
          if (isObject(value) && value.type === 'control_response' && isObject(value.response)) {
            const response = value.response as Record<string, any>;
            if (response.request_id === interruptId && (protocol.result || !promptSent)) {
              toolSettled = true; child.stdin.end();
              continue;
            } else if (response.request_id === initializeId) {
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
              if (cancelled || signal.aborted) stop();
              else { await send(userRecord(plan.textForm ? { ...request, input: { role: 'user', ...textHistory(request.history, request.input) } } : request)); promptSent = true; }
            }
          } else {
            if (plan.resume && !protocol.started && !events.producedOutput && isObject(value) && value.type === 'result' && value.is_error === true &&
              isRefusal([value.result, ...(Array.isArray(value.errors) ? value.errors : [])].join('\n'))) {
              resumeRefused = true; child.stdin.end(); endTimer = setTimeout(() => kill('SIGKILL'), fixedWait(2000)); break;
            }
            if (plan.resume && isObject(value) && value.type === 'system' && value.subtype === 'init') { pendingInit = value; clearTimeout(startTimer); }
            else {
              if (pendingInit) { await protocol.accept(pendingInit); pendingInit = undefined; }
              await protocol.accept(value);
            }
          }
          if (protocol.started) clearTimeout(startTimer);
          if (isObject(value) && value.type === 'result' && protocol.result && (!protocol.background || protocol.error)) {
            toolSettled = true; child.stdin.end(); endTimer = setTimeout(() => kill('SIGKILL'), fixedWait(2000));
            break;
          }
        }
        const exit = await boundedClose();
        resumeRefused ||= plan.resume && !protocol.started && !events.producedOutput && isRefusal(stderr);
        if (!resumeRefused) {
          if (spawnError) throw new Failure('tool_failed', 'Claude could not start.');
          if (protocol.error) throw protocol.error;
          if (!protocol.result && !cancelled) throw new Failure('tool_failed', `Claude ended without a result${stderr ? ': ' + stderr : '.'}`, {
            detail: { basis: 'fields', ...(exit.code === null ? {} : { toolExit: exit.code }), ...(stderr ? { excerpt: stderr } : {}) } }, secrets);
          if (!promptSent && !cancelled) throw new Failure('tool_unsupported', 'Claude did not complete its settings handshake.');
        }
      } catch (failure) {
        kill('SIGKILL'); await boundedClose();
        if (plan.resume && !protocol.started && !events.producedOutput && isRefusal(stderr)) resumeRefused = true;
        else error ??= failure;
      }
      finally {
        clearTimeout(startTimer); clearTimeout(stopTimer); clearTimeout(endTimer);
        signal.removeEventListener('abort', stop); events.output.off('error', outputError);
        stdout.close(); stopActive = undefined;
        kill('SIGKILL');
        try {
          const remaining = await (cleanup ?? endRunGroups(temp, record.tool, stopDeadline));
          if (remaining.length) {
            error ??= new Failure('internal', 'Could not end process groups: ' + remaining.join(', '));
            await events.emit({ type: 'notice', code: 'cleanup_incomplete', message: 'Could not end process groups: ' + remaining.join(', ') });
          }
        } catch (failure) { error ??= failure; }
        clearTimeout(deadlineTimer);
      }
      if (resumeRefused && !error && !cancelled && !signal.aborted && attempt === 0) {
        plan.continuity = 'rebuilt'; plan.rebuildReason ??= 'resume_refused'; plan.conversation = randomUUID();
        plan.resume = false; plan.textForm = true;
        record.conversation = plan.conversation; delete record.tool; await runs.save(record);
        continue;
      }
      if (resumeRefused && !error) error = new Failure('tool_failed', 'Claude refused the conversation.');
      break;
    }
  } catch (failure) { error ??= failure; cancelled ||= signal.aborted; }
  finally {
    input?.close(); if (controlTask) await controlTask;
    if (request.workingDirectory === undefined && plan.syntheticFile) await rm(plan.syntheticFile, { force: true }).catch(failure => { error ??= failure; });
    await rm(temp, { recursive: true, force: true }).catch(failure => { error ??= failure; });
  }
  cancelled ||= signal.aborted;
  try { await chats.settle(request, plan, fingerprint.snapshot(), !error && !cancelled, home); }
  catch (failure) {
    error ??= failure;
    await chats.settle(request, plan, fingerprint.snapshot(), false, home).catch(() => {});
  }
  if (signal.aborted && !cancelled) {
    cancelled = true;
    await chats.settle(request, { ...plan, continuedConversation: plan.conversation }, fingerprint.snapshot(), false, home);
  }
  events.onText = undefined;
  try { return await events.finish(error, cancelled); }
  catch (failure) { await chats.settle(request, { ...plan, continuedConversation: plan.conversation }, fingerprint.snapshot(), false, home); throw failure; }
  finally { await runs.remove(request.runId); }
  } finally { input?.close(); }
}
