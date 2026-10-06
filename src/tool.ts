import { access, stat, mkdtemp, rm } from 'node:fs/promises';
import { constants } from 'node:fs';
import { resolve, join, delimiter } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { Failure, errno } from './errors.js';
import { cleanEnvironment } from './environment.js';
import { isObject } from './settings.js';
import { privateDirectory, readJson, writeJson } from './state.js';
import type { State } from './state.js';
import type { ProcessEnv, ToolInfo, ToolRecord } from './types.js';
const exec = promisify(execFile);
export const OLDEST_SUPPORTED = '2.1.289';
export function supportedVersion(version: string): boolean {
  const parts = /^(\d+)\.(\d+)\.(\d+)$/.exec(version);
  if (!parts) return false;
  const wanted = OLDEST_SUPPORTED.split('.').map(Number);
  for (let i = 0; i < 3; i++) {
    const found = BigInt(parts[i + 1]!);
    const minimum = BigInt(wanted[i]!);
    if (found !== minimum) return found > minimum;
  }
  return true;
}
async function executable(path: string): Promise<boolean> {
  try { await access(path, constants.X_OK); return (await stat(path)).isFile(); }
  catch (error) { if (errno(error, 'ENOENT') || errno(error, 'ENOTDIR') || errno(error, 'EACCES')) return false; throw error; }
}
export async function findTool(toolPath: string | undefined, env: ProcessEnv): Promise<string | undefined> {
  const program = toolPath ?? 'claude';
  if (program.includes('/') || program.includes('\\')) {
    const path = resolve(program);
    return await executable(path) ? path : undefined;
  }
  for (const folder of (env.PATH ?? '').split(delimiter)) {
    const path = resolve(folder || '.', program);
    if (await executable(path)) return path;
  }
  return undefined;
}
export async function inspectTool(state: State, toolPath: string | undefined, env: ProcessEnv, signal?: AbortSignal): Promise<ToolInfo> {
  const info: ToolInfo = { name: 'claude', supported: false, oldestSupported: OLDEST_SUPPORTED };
  const path = await findTool(toolPath, env);
  if (path === undefined) return info;
  info.path = path;
  const metadata = await stat(path);
  const changedAt = metadata.mtime.toISOString();
  const cached = await readJson(join(state.directory, 'tool.json'));
  let version: string;
  if (isObject(cached) && cached.path === path && cached.size === metadata.size && cached.changedAt === changedAt &&
    typeof cached.version === 'string' && /^\d+\.\d+\.\d+$/.test(cached.version)) {
    version = cached.version;
  } else {
    const tempDirectory = join(state.directory, 'tmp');
    await privateDirectory(tempDirectory);
    const home = await mkdtemp(join(tempDirectory, 'version-'));
    try {
      let output: string;
      try {
        const result = await exec(path, ['--version'], {
          env: cleanEnvironment(env, home, join(home, '.claude')), cwd: home,
          timeout: 10000, maxBuffer: 16 * 1024, ...(signal ? { signal } : {}),
        });
        output = result.stdout;
      } catch (error) {
        if (errno(error, 'ENOENT')) throw new Failure('tool_missing', `claude was not found; version not found, needed ${OLDEST_SUPPORTED}.`, { detail: { basis: 'fields', neededVersion: OLDEST_SUPPORTED }, remedy: 'Install claude.' });
        throw new Failure('tool_failed', `claude could not report its version at ${path}.`, { detail: { basis: 'fields', neededVersion: OLDEST_SUPPORTED } });
      }
      const match = /^\s*(\d+\.\d+\.\d+)\s+\(Claude Code\)\s*$/.exec(output);
      if (!match) throw new Failure('tool_failed', 'claude returned an unrecognised version record.', { detail: { basis: 'fields', neededVersion: OLDEST_SUPPORTED } });
      version = match[1]!;
      const record: ToolRecord = { path, size: metadata.size, changedAt, version, askedAt: new Date().toISOString() };
      await writeJson(join(state.directory, 'tool.json'), record);
    } finally { await rm(home, { recursive: true, force: true }); }
  }
  info.version = version;
  info.supported = supportedVersion(version);
  return info;
}
export function requireTool(tool: ToolInfo): void {
  if (!tool.path) throw new Failure('tool_missing', `claude was not found; version not found, needed ${OLDEST_SUPPORTED}.`, { detail: { basis: 'fields', neededVersion: OLDEST_SUPPORTED }, remedy: 'Install claude.' });
  if (!tool.supported) throw new Failure('tool_unsupported', `claude ${tool.version ?? 'unknown'} is unsupported; needed ${OLDEST_SUPPORTED} or newer.`, { detail: { basis: 'fields', toolVersion: tool.version ?? 'unknown', neededVersion: OLDEST_SUPPORTED }, remedy: 'Update claude.' });
}
