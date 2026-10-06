import { chatKey } from './history.js';
import { link, readdir, rm, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure, errno } from './errors.js';
import { isObject } from './settings.js';
import { ownerAlive, parseOwner, prepareFile, processOwner, readJson, writeJson } from './state.js';
import type { State } from './state.js';
import type { Owner } from './types.js';
import type { RunRequest } from './run-request.js';
export interface RunRecord { runId: string; subscriptionId: string; owner: Owner; startedAt: string; labels?: Record<string, string>; conversation?: string; chatKey?: string; tool?: Owner }
export class Runs {
  constructor(readonly state: State) {}
  path(id: string): string {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(id)) throw new Failure('invalid_input', 'The run identifier is not a safe file name.');
    return join(this.state.directory, 'runs', id + '.json');
  }
  async list(): Promise<RunRecord[]> {
    const files = await readdir(join(this.state.directory, 'runs')).catch(error => { if (errno(error, 'ENOENT')) return []; throw error; });
    const result: RunRecord[] = [];
    for (const file of files) {
      if (!file.endsWith('.json') || file.startsWith('.')) continue;
      const record = await readJson(join(this.state.directory, 'runs', file));
      if (record === undefined) continue;
      if (!isObject(record) || typeof record.runId !== 'string' || file !== record.runId + '.json') throw new Failure('internal', 'Malformed run record.');
      if (await ownerAlive(parseOwner(record.owner))) result.push(record as unknown as RunRecord);
      else {
        if (record.tool !== undefined && await ownerAlive(parseOwner(record.tool))) {
          try { process.kill(-parseOwner(record.tool).pid, 'SIGKILL'); } catch { /* gone */ }
        }
        await rm(join(this.state.directory, 'tmp', record.runId), { recursive: true, force: true });
        await rm(this.path(record.runId), { force: true });
      }
    }
    return result;
  }
  async reserve(request: RunRequest, conversation: string): Promise<RunRecord> {
    await this.list();
    const owner = await processOwner(process.pid);
    if (!owner) throw new Failure('internal', 'Cannot identify the run owner.');
    const record = { runId: request.runId, subscriptionId: request.subscriptionId, owner,
      startedAt: new Date().toISOString(), ...(request.labels ? { labels: request.labels } : {}), conversation, ...(request.chatSessionId ? { chatKey: chatKey(request.chatSessionId) } : {}) };
    const path = this.path(request.runId);
    const temporary = await prepareFile(path, JSON.stringify(record) + '\n');
    try { await link(temporary, path); }
    catch (error) { if (errno(error, 'EEXIST')) throw new Failure('in_use', 'That run identifier is already in use.'); throw error; }
    finally { await unlink(temporary); }
    return record;
  }
  save(record: RunRecord): Promise<void> { return writeJson(this.path(record.runId), record); }
  remove(id: string): Promise<void> { return rm(this.path(id), { force: true }); }
}
