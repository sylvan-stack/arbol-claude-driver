import { homedir } from 'node:os';
import { resolve } from 'node:path';
import { parseArguments } from './arguments.js';
import { Failure, safeFailure } from './errors.js';
import { readSettings, settingsPath } from './settings.js';
import { buildIdentity } from './build.js';
import { State, defaultStateDirectory, fixedWait } from './state.js';
import { inspectTool, requireTool } from './tool.js';
import { standIn } from './stand-in.js';
export async function main(args: string[]): Promise<number> {
  const cancellation = new AbortController();
  const stop = (): void => cancellation.abort();
  process.on('SIGINT', stop);
  process.on('SIGTERM', stop);
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
    if (command.name === 'info') {
      const tool = await inspectTool(state, settings.toolPath, process.env, cancellation.signal);
      cancellation.signal.throwIfAborted();
      process.stdout.write(JSON.stringify({ driver: 'claude', ...identity, contract: 1, stateDirectory: state.directory, settingsFile: file, tool, ...(notices.length ? { notices } : {}) }) + '\n');
      return 0;
    }
    if (command.toolRequired) requireTool(await inspectTool(state, settings.toolPath, process.env, cancellation.signal));
    throw new Failure('unsupported', `Command ${command.name} is not implemented in this Driver yet.`);
  } catch (error) {
    if (cancellation.signal.aborted) return 2;
    process.stdout.write(JSON.stringify({ error: safeFailure(error) }) + '\n');
    return 1;
  } finally {
    process.off('SIGINT', stop);
    process.off('SIGTERM', stop);
  }
}
