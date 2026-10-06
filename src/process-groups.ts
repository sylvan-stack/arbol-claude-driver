import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile } from 'node:fs/promises';
import { basename, join } from 'node:path';
import { errno } from './errors.js';
import { fixedWait, privateDirectory } from './state.js';
import type { Owner, ProcessEnv } from './types.js';
const exec = promisify(execFile);
const quote = (value: string): string => "'" + value.replaceAll("'", "'\\''") + "'";
const identity = 'stamp=$(/usr/bin/env -i PATH=/usr/bin:/bin LC_ALL=C TZ=UTC /bin/ps -p "$$" -o lstart=)\n';
export async function runLaunchers(folder: string, env: ProcessEnv): Promise<{ launcher: string; shell: string }> {
  await privateDirectory(folder);
  const realShell = env.SHELL && ['zsh', 'bash'].includes(basename(env.SHELL)) ? env.SHELL : '/bin/zsh';
  const shell = join(folder, basename(realShell));
  await writeFile(shell, '#!/bin/sh\n' + identity + `printf '%s\\t%s\\n' "$$" "$stamp" >> ${quote(join(folder, 'shell-calls'))}\n` +
    `export SHELL=${quote(realShell)}\nexec ${quote(realShell)} "$@"\n`, { mode: 0o700 });
  const launcher = join(folder, 'launch-tool');
  await writeFile(launcher, '#!/bin/sh\n' + identity + `printf '%s\\t%s\\n' "$$" "$stamp" > ${quote(join(folder, 'tool-owner'))}\nexec "$@"\n`, { mode: 0o700 });
  return { launcher, shell };
}
export async function notedOwners(path: string): Promise<Owner[]> {
  const content = await readFile(path, 'utf8').catch(error => { if (errno(error, 'ENOENT')) return ''; throw error; });
  return content.split('\n').filter(Boolean).map(line => {
    const [pid, time] = line.split('\t'); const started = Date.parse(time!.trim() + ' UTC');
    if (!/^[1-9][0-9]*$/.test(pid!) || !Number.isFinite(started)) throw new Error('Malformed process ledger.');
    return { pid: Number(pid), startedAt: new Date(started).toISOString() };
  });
}
interface GroupProcess extends Owner { group: number; zombie: boolean }
async function processes(deadline: number): Promise<GroupProcess[]> {
  const { stdout } = await exec('/bin/ps', ['-axo', 'pid=,pgid=,stat=,lstart='], {
    env: { PATH: '/usr/bin:/bin', LC_ALL: 'C', TZ: 'UTC' }, timeout: Math.max(1, Math.min(1000, deadline - performance.now())), maxBuffer: 4 * 1024 * 1024,
  });
  return stdout.split('\n').flatMap(line => {
    const match = /^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.+)$/.exec(line); if (!match) return [];
    const started = Date.parse(match[4] + ' UTC'); if (!Number.isFinite(started)) throw new Error('Unreadable process time.');
    return [{ pid: Number(match[1]), group: Number(match[2]), zombie: match[3]!.startsWith('Z'), startedAt: new Date(started).toISOString() }];
  });
}
// A group leader may have exited while its background children still own the group.
// A present leader with a different start time is a reused PID and is never signalled.
export function remainingGroups(owners: Owner[], table: GroupProcess[]): number[] {
  return [...new Set(owners.filter(owner => {
    const leader = table.find(process => process.pid === owner.pid);
    if (leader && Math.abs(Date.parse(leader.startedAt) - Date.parse(owner.startedAt)) > 2000) return false;
    return table.some(process => process.group === owner.pid && !process.zombie);
  }).map(owner => owner.pid))];
}
export async function endGroups(owners: Owner[], deadline = performance.now() + fixedWait(15000)): Promise<number[]> {
  if (!owners.length) return [];
  if (performance.now() >= deadline) return [...new Set(owners.map(owner => owner.pid))];
  let remaining = remainingGroups(owners, await processes(deadline));
  const signal = (groups: number[], name: NodeJS.Signals): void => {
    for (const group of groups) { try { process.kill(-group, name); } catch (error) { if (!errno(error, 'ESRCH')) throw error; } }
  };
  signal(remaining, 'SIGTERM');
  const grace = Math.min(deadline, performance.now() + fixedWait(2000));
  while (remaining.length && performance.now() < deadline) {
    if (performance.now() >= grace) {
      if (performance.now() >= deadline) break;
      const verified = await processes(deadline).catch(error => { if (performance.now() >= deadline) return undefined; throw error; });
      if (!verified) break;
      signal(remainingGroups(owners, verified), 'SIGKILL');
    }
    await new Promise(resolve => setTimeout(resolve, Math.min(fixedWait(50), Math.max(1, deadline - performance.now()))));
    if (performance.now() >= deadline) break;
    const next = await processes(deadline).catch(error => { if (performance.now() >= deadline) return undefined; throw error; });
    if (!next) break;
    remaining = remainingGroups(owners, next);
  }
  return remaining;
}
export async function endRunGroups(folder: string, tool?: Owner, deadline?: number): Promise<number[]> {
  const owners = [...await notedOwners(join(folder, 'shell-calls')), ...await notedOwners(join(folder, 'tool-owner')), ...(tool ? [tool] : [])];
  return endGroups(owners, deadline);
}
