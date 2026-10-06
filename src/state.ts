import { randomUUID } from 'node:crypto';
import { open, readFile, mkdir, chmod, rename, unlink, link, stat, readdir } from 'node:fs/promises';
import { dirname, join, resolve } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { Failure, errno } from './errors.js';
import { isObject } from './settings.js';
import type { Owner, ProcessEnv } from './types.js';
const exec = promisify(execFile);
export const DATA_FORMAT = 1;
export const defaultStateDirectory = (home: string): string => join(home, 'Library', 'Application Support', 'Arbol', 'drivers', 'claude');
export function fixedWait(milliseconds: number, env: ProcessEnv = process.env): number {
  if (env.ARBOL_TEST_TIME_SCALE === undefined) return milliseconds;
  const scale = Number(env.ARBOL_TEST_TIME_SCALE);
  if (!Number.isFinite(scale) || scale <= 0) throw new Failure('invalid_input', 'ARBOL_TEST_TIME_SCALE must be a positive finite number.');
  return milliseconds / scale;
}
export async function privateDirectory(path: string): Promise<void> {
  await mkdir(path, { recursive: true, mode: 0o700 });
  await chmod(path, 0o700);
}
// Temporary files remain in the target folder, so rename never crosses a filesystem.
export async function prepareFile(path: string, content: string): Promise<string> {
  await privateDirectory(dirname(path));
  const temp = join(dirname(path), `.write-${randomUUID()}`);
  const file = await open(temp, 'wx', 0o600);
  try { await file.writeFile(content, 'utf8'); await file.sync(); }
  catch (error) { await unlink(temp).catch(() => {}); throw error; }
  finally { await file.close(); }
  return temp;
}
export async function atomicWrite(path: string, content: string): Promise<void> {
  const temp = await prepareFile(path, content);
  try { await rename(temp, path); }
  finally { await unlink(temp).catch((error: unknown) => { if (!errno(error, 'ENOENT')) throw error; }); }
}
export const writeJson = async (path: string, value: unknown): Promise<void> => atomicWrite(path, JSON.stringify(value) + '\n');
export async function readJson(path: string): Promise<unknown | undefined> {
  try { return JSON.parse(await readFile(path, 'utf8')) as unknown; }
  catch (error) {
    if (errno(error, 'ENOENT')) return undefined;
    throw new Failure('internal', `Cannot read state file ${path}.`);
  }
}
export function parseOwner(value: unknown): Owner {
  if (!isObject(value) || !Number.isInteger(value.pid) || (value.pid as number) <= 0 ||
    typeof value.startedAt !== 'string' || !Number.isFinite(Date.parse(value.startedAt))) {
    throw new Failure('internal', 'A process owner record is malformed.');
  }
  return { pid: value.pid as number, startedAt: value.startedAt };
}
export async function processOwner(pid: number): Promise<Owner | undefined> {
  try {
    const { stdout } = await exec('/bin/ps', ['-p', String(pid), '-o', 'lstart='], {
      env: { PATH: '/usr/bin:/bin', LC_ALL: 'C', TZ: 'UTC' }, timeout: 5000, maxBuffer: 4096,
    });
    const started = Date.parse(stdout.trim() + ' UTC');
    if (!Number.isFinite(started)) throw new Failure('internal', `Cannot read the start time of process ${pid}.`);
    return { pid, startedAt: new Date(started).toISOString() };
  } catch (error) {
    if (isObject(error) && error.code === 1) return undefined; // ps: no such process
    throw error;
  }
}
export async function ownerAlive(owner: Owner): Promise<boolean> {
  const current = await processOwner(owner.pid);
  return current !== undefined && Math.abs(Date.parse(current.startedAt) - Date.parse(owner.startedAt)) <= 2000;
}
export class State {
  readonly directory: string;
  constructor(directory: string) { this.directory = resolve(directory); }
  async initialize(): Promise<void> {
    const path = join(this.directory, 'format');
    const check = async (): Promise<boolean> => {
      let content: string;
      try { content = await readFile(path, 'utf8'); }
      catch (error) { if (errno(error, 'ENOENT')) return false; throw error; }
      const value = content.trim();
      if (!/^[1-9][0-9]*$/.test(value)) throw new Failure('internal', `State format in ${path} is malformed.`);
      const number = BigInt(value);
      if (number > BigInt(DATA_FORMAT)) throw new Failure('state_newer', `State format ${number} is newer than supported format ${DATA_FORMAT}.`, { remedy: 'Run the Build that wrote this state, or a newer one.' });
      return true;
    };
    if (await check()) { await privateDirectory(this.directory); await chmod(path, 0o600); return; }
    await privateDirectory(this.directory);
    const temp = await prepareFile(path, `${DATA_FORMAT}\n`);
    try { await link(temp, path); }
    catch (error) { if (!errno(error, 'EEXIST')) throw error; }
    finally { await unlink(temp); }
    await check();
  }
  subscription(id: string): string {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(id)) throw new Failure('invalid_input', 'The Subscription identifier is not a safe file name.');
    return join(this.directory, 'subscriptions', id);
  }
  async clearUnfinishedWrites(): Promise<void> {
    const visit = async (folder: string, depth: number): Promise<void> => {
      const entries = await readdir(folder, { withFileTypes: true }).catch(error => { if (errno(error, 'ENOENT')) return []; throw error; });
      for (const entry of entries) {
        const path = join(folder, entry.name);
        if (entry.isFile() && entry.name.startsWith('.write-')) {
          const info = await stat(path).catch(error => { if (errno(error, 'ENOENT')) return undefined; throw error; });
          if (info && Date.now() - info.mtimeMs > 60000) await unlink(path).catch(error => { if (!errno(error, 'ENOENT')) throw error; });
        } else if (depth > 0 && entry.isDirectory() && entry.name !== 'home' && entry.name !== 'tmp') await visit(path, depth - 1);
      }
    };
    await visit(this.directory, 3);
  }
  async withSubscriptionLock<T>(id: string, change: () => Promise<T>, env: ProcessEnv = process.env): Promise<T> {
    const directory = this.subscription(id);
    await privateDirectory(join(this.directory, 'subscriptions'));
    await privateDirectory(directory);
    const path = join(directory, 'lock');
    const owner = await processOwner(process.pid);
    if (!owner) throw new Failure('internal', 'Cannot identify the Driver process.');
    const temporary = await prepareFile(path, JSON.stringify({ owner }) + '\n');
    const deadline = performance.now() + fixedWait(10000, env);
    let takenBy: Owner | undefined;
    let acquired = false;
    try {
      while (!acquired) {
        try { await link(temporary, path); acquired = true; }
        catch (error) {
          if (!errno(error, 'EEXIST')) throw error;
          const handle = await open(path, 'r').catch((error: unknown) => { if (errno(error, 'ENOENT')) return undefined; throw error; });
          if (!handle) continue;
          let snapshot;
          try {
            snapshot = await handle.stat();
            let existing: unknown;
            try { existing = JSON.parse(await handle.readFile('utf8')) as unknown; }
            catch { throw new Failure('internal', `Malformed lock ${path}.`); }
            if (!isObject(existing)) throw new Failure('internal', `Malformed lock ${path}.`);
            takenBy = parseOwner(existing.owner);
          } finally { await handle.close(); }
          if (!(await ownerAlive(takenBy))) {
            // Elect one reaper per stale inode. New lock owners have a different inode.
            const reclaim = join(directory, `.write-reap-${snapshot.ino}`);
            let reaping = false;
            try {
              await link(temporary, reclaim);
              reaping = true;
              const current = await stat(path).catch((error: unknown) => { if (errno(error, 'ENOENT')) return undefined; throw error; });
              if (current?.ino === snapshot.ino) await unlink(path);
            } catch (error) {
              if (errno(error, 'EEXIST')) {
                const reaper = await readJson(reclaim);
                if (isObject(reaper) && !(await ownerAlive(parseOwner(reaper.owner)))) {
                  // A reaper killed before unlinking the stale lock must not strand it.
                  await unlink(reclaim).catch((failure: unknown) => { if (!errno(failure, 'ENOENT')) throw failure; });
                }
              } else if (!errno(error, 'ENOENT')) throw error;
            }
            finally { if (reaping) await unlink(reclaim).catch(() => {}); }
          }
          if (performance.now() >= deadline) throw new Failure('internal', `Subscription lock timed out after ${fixedWait(10000, env)} milliseconds; owner PID ${takenBy.pid}, started ${takenBy.startedAt}.`);
          await new Promise<void>(resolve => setTimeout(resolve, fixedWait(50, env)));
        }
      }
      return await change();
    } finally {
      if (acquired) await unlink(path);
      await unlink(temporary);
    }
  }
}
