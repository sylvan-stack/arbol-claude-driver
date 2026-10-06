import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { parseArguments } from './arguments.js';
import { Failure, safeFailure } from './errors.js';
import { readSettings, settingsPath } from './settings.js';
import { buildIdentity } from './build.js';
import { State, defaultStateDirectory, fixedWait } from './state.js';
import { inspectTool, requireTool } from './tool.js';
import { standIn } from './stand-in.js';
import { Subscriptions, families, familyId } from './subscriptions.js';
import { Authentication } from './authentication.js';
import { RunEvents } from './run-events.js';
import { run } from './run.js';
import { Runs } from './runs.js';
export async function main(args: string[]): Promise<number> {
  const operationWords = args.filter((word, index) => word !== '--state-dir' && !word.startsWith('--state-dir=') && args[index - 1] !== '--state-dir');
  const isRun = operationWords[0] === 'run';
  const runEvents = isRun ? new RunEvents(process.stdout, args.includes('--output=text') || args[args.indexOf('--output') + 1] === 'text') : undefined;
  // A broken caller pipe must reach run cleanup rather than crash the Driver.
  const outputFailure = (): void => cancellation.abort();
  const cancellation = new AbortController();
  const stop = (): void => cancellation.abort();
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
  if (runEvents) process.stdout.on('error', outputFailure);
  try {
    if (Number(process.versions.node.split('.')[0]) < 24) throw new Failure('unsupported', 'This Driver needs Node.js 24 or later.');
    const command = parseArguments(args);
    // This executable is also the stand-in, which deliberately reads no Driver state.
    if (command.name === 'dev stand-in') return await standIn(resolve(command.positionals[0]!), command.positionals.slice(2));
    fixedWait(1); // Validate the optional test scale before any state mutation.
    const home = process.env.HOME || homedir();
    const file = settingsPath(home);
    const { settings, notices } = await readSettings(file);
    const state = new State(typeof command.options['state-dir'] === 'string' ? command.options['state-dir'] : defaultStateDirectory(home));
    await state.initialize();
    const identity = await buildIdentity();
    cancellation.signal.throwIfAborted();
    if (command.name === 'run' || command.name.startsWith('runs ') || ['subscriptions create', 'subscriptions update', 'subscriptions sign-in', 'subscriptions sign-out', 'subscriptions remove', 'subscriptions import', 'subscriptions check', 'subscriptions usage'].includes(command.name)) await new Runs(state).list();
    if (command.name === 'info') {
      const tool = await inspectTool(state, settings.toolPath, process.env, cancellation.signal);
      cancellation.signal.throwIfAborted();
      process.stdout.write(JSON.stringify({ driver: 'claude', ...identity, contract: 1, stateDirectory: state.directory, settingsFile: file, tool, ...(notices.length ? { notices } : {}) }) + '\n');
      return 0;
    }
    if (command.name === 'run') return await run(command, new Subscriptions(state, settings, notices, file), runEvents!, process.env, identity.build, cancellation.signal);
    if (command.name === 'runs list') {
      const records = await new Runs(state).list();
      process.stdout.write(JSON.stringify(records.map(({ runId, subscriptionId, startedAt, labels }) => ({ runId, subscriptionId, startedAt, ...(labels ? { labels } : {}) }))) + '\n');
      return 0;
    }
    if (command.name === 'runs stop') { await new Runs(state).stop(command.positionals[0]!); return 0; }
    if (command.name.startsWith('subscriptions ')) {
      const subscriptions = new Subscriptions(state, settings, notices, file);
      const authenticate = (): Promise<Authentication> => Authentication.create(subscriptions, process.env, cancellation.signal);
      let result: unknown;
      switch (command.name) {
        case 'subscriptions families': result = families; break;
        case 'subscriptions list': result = await subscriptions.list(); break;
        case 'subscriptions create': {
          if (typeof command.options.label !== 'string') throw new Failure('invalid_input', 'Give the new Subscription a label.');
          result = await subscriptions.create(familyId(command.options.family), command.options.label, command.options.id as string | undefined);
          break;
        }
        case 'subscriptions update':
          await (await authenticate()).cleanProgress(command.positionals[0]!);
          result = await subscriptions.update(command.positionals[0]!, command.options.label as string | undefined); break;
        case 'subscriptions models':
          await subscriptions.requireSignedIn(command.positionals[0]!);
          result = await (await authenticate()).models(command.positionals[0]!); break;
        case 'subscriptions check': {
          const subscription = await subscriptions.get(command.positionals[0]!);
          if (subscription.state === 'signed_out') throw new Failure('not_signed_in', 'Sign in to this Subscription first.', { remedy: 'Sign in.' });
          result = await (await authenticate()).check(command.positionals[0]!); break;
        }
        case 'subscriptions usage': {
          const subscription = await subscriptions.get(command.positionals[0]!);
          if (subscription.state !== 'signed_in') result = { outcome: 'needs_sign_in', message: 'Sign in to this Subscription first.', fetchedAt: new Date().toISOString(), windows: [] };
          else if (subscription.family === 'anthropic') result = subscription.lastUsage ?? { outcome: 'unsupported', message: 'Usage is available after a run reports it.', fetchedAt: new Date().toISOString(), windows: [] };
          else result = await (await authenticate()).usage(command.positionals[0]!);
          break;
        }
        case 'subscriptions sign-in': return await (await authenticate()).signIn(command.positionals[0]!, command.options['by-code'] === true,
          step => { process.stdout.write(JSON.stringify(step) + '\n'); });
        case 'subscriptions sign-out': result = await (await authenticate()).signOut(command.positionals[0]!, command.options.force === true); break;
        case 'subscriptions remove': await (await authenticate()).signOut(command.positionals[0]!, command.options.force === true, true); return 0;
        case 'subscriptions import':
          if (typeof command.options.from !== 'string') throw new Failure('invalid_input', 'Name the settings file with --from.');
          result = await (await authenticate()).import(command.positionals[0]!, resolve(command.options.from)); break;
      }
      if (result !== undefined) {
        cancellation.signal.throwIfAborted();
        // Arrays keep the contract's array shape; item notices describe family faults.
        if (notices.length && result !== null && typeof result === 'object') {
          if (Array.isArray(result)) result = result.map(item => ({ ...item, notices }));
          else result = { ...result, notices };
        }
        process.stdout.write(JSON.stringify(result) + '\n');
        return 0;
      }
    }
    if (command.toolRequired) requireTool(await inspectTool(state, settings.toolPath, process.env, cancellation.signal));
    throw new Failure('unsupported', `Command ${command.name} is not implemented in this Driver yet.`);
  } catch (error) {
    if (runEvents) { try { return await runEvents.finish(error, cancellation.signal.aborted); } catch { return 2; } }
    if (cancellation.signal.aborted) return 2;
    process.stdout.write(JSON.stringify({ error: safeFailure(error) }) + '\n');
    return 1;
  } finally {
    process.off('SIGINT', stop);
    process.off('SIGTERM', stop);
    if (runEvents) process.stdout.off('error', outputFailure);
  }
}
