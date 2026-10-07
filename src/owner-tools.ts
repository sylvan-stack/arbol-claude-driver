import { lstat, mkdir, readdir, readlink, rename, rm, stat, symlink } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';
import { Failure, errno } from './errors.js';

async function exists(path: string, directory: boolean): Promise<boolean> {
  try { const info = await stat(path); return directory ? info.isDirectory() : info.isFile(); }
  catch (error) { if (errno(error, 'ENOENT')) return false; throw error; }
}
export async function ownerSkills(home: string, ownerHome: string): Promise<void> {
  const source = join(ownerHome, '.claude', 'skills'), directory = join(home, 'skills');
  const current = await lstat(directory).catch(error => { if (errno(error, 'ENOENT')) return undefined; throw error; });
  if (current?.isSymbolicLink()) await rm(directory); // remove the link, never its target
  else if (current && !current.isDirectory()) throw new Failure('internal', 'The Subscription skills entry is not a directory.');
  await mkdir(directory, { recursive: true, mode: 0o700 });
  const names = await exists(source, true) ? await readdir(source) : [];
  const desired = new Set<string>();
  for (const name of names) if (name !== 'synced' && await exists(join(source, name), true)) desired.add(name);
  // Only root symlinks belong to this adapter. Claude owns skills/synced.
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    if (entry.name !== 'synced' && entry.isSymbolicLink() && !desired.has(entry.name)) await rm(join(directory, entry.name), { force: true });
  }
  for (const name of desired) {
    const target = join(source, name), link = join(directory, name);
    const entry = await lstat(link).catch(error => { if (errno(error, 'ENOENT')) return undefined; throw error; });
    if (entry && !entry.isSymbolicLink()) throw new Failure('internal', 'An owner skill conflicts with a private skill entry.');
    if (entry && await readlink(link) === target) continue;
    const temporary = join(directory, '.arbol-skill-' + randomUUID());
    try { await symlink(target, temporary); await rename(temporary, link); }
    finally { await rm(temporary, { force: true }); }
  }
}
export async function ownerMcpArguments(ownerHome: string): Promise<string[]> {
  const file = join(ownerHome, '.claude.json');
  return await exists(file, false) ? ['--mcp-config', file] : [];
}
