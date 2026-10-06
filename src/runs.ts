import { chatKey } from './history.js';
import { link, readdir, rm, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure, errno } from './errors.js';
import { isObject } from './settings.js';
import { fixedWait, ownerAlive, parseOwner, prepareFile, processOwner, readJson, writeJson } from './state.js';
import { endRunGroups } from './process-groups.js';
import type { State } from './state.js';
import type { Owner } from './types.js';
import type { RunRequest } from './run-request.js';
export interface RunRecord { runId: string; subscriptionId: string; owner: Owner; startedAt: string; labels?: Record<string, string>; conversation?: string; continuedConversation?: string; chatKey?: string; tool?: Owner; cleanupOwner?: Owner }
export class Runs {
  constructor(readonly state: State) {}
  path(id: string): string {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(id)) throw new Failure('invalid_input', 'The run identifier is not a safe file name.');
    return join(this.state.directory, 'runs', id + '.json');
  }
  async list(recover = true): Promise<RunRecord[]> {
    const files = await readdir(join(this.state.directory, 'runs')).catch(error => { if (errno(error, 'ENOENT')) return []; throw error; });
    const result: RunRecord[] = [];
    for (const file of files) {
      if (!file.endsWith('.json') || file.startsWith('.')) continue;
      const record = await readJson(join(this.state.directory, 'runs', file));
      if (record === undefined) continue;
      if (!isObject(record) || typeof record.runId !== 'string' || file !== record.runId + '.json' || typeof record.subscriptionId !== 'string') throw new Failure('internal', 'Malformed run record.');
      this.path(record.runId); this.state.subscription(record.subscriptionId);
      if (await ownerAlive(parseOwner(record.owner))) result.push(record as unknown as RunRecord);
      else if (recover) {
        await this.recover(record as unknown as RunRecord);
      }
    }
    return result;
  }
  private async recover(stale: RunRecord): Promise<void> {
    const owner = (await processOwner(process.pid))!;
    const deadline = performance.now() + fixedWait(20000);
    while (true) {
      const claimed = await this.state.withSubscriptionLock(stale.subscriptionId, async () => {
        const current = await readJson(this.path(stale.runId)) as RunRecord | undefined;
        if (!current || current.owner.pid !== stale.owner.pid || current.owner.startedAt !== stale.owner.startedAt || await ownerAlive(parseOwner(current.owner))) return 'gone';
        if (current.cleanupOwner && await ownerAlive(parseOwner(current.cleanupOwner))) return 'waiting';
        current.cleanupOwner = owner; await this.save(current); return current;
      });
      if (claimed === 'gone') return;
      if (claimed === 'waiting') {
        if (performance.now() >= deadline) throw new Failure('internal', 'Another call could not finish recovering the run.');
        await new Promise(resolve => setTimeout(resolve, fixedWait(50))); continue;
      }
      try {
        const folder = join(this.state.directory, 'tmp', stale.runId);
        const remaining = await endRunGroups(folder, claimed.tool && parseOwner(claimed.tool));
        if (remaining.length) throw new Failure('internal', 'Could not end orphaned process groups: ' + remaining.join(', '));
        await rm(folder, { recursive: true, force: true });
        await this.state.withSubscriptionLock(stale.subscriptionId, async () => {
          if (claimed.chatKey && claimed.continuedConversation) {
            if (!/^[a-f0-9]{64}$/.test(claimed.chatKey)) throw new Failure('internal', 'Malformed chat key.');
            const mapPath = join(this.state.subscription(stale.subscriptionId), 'chats', claimed.chatKey + '.json');
            const entry = await readJson(mapPath);
            if (isObject(entry) && entry.conversation === claimed.continuedConversation) await rm(mapPath, { force: true });
          }
          await this.remove(stale.runId);
        });
      } catch (error) {
        delete claimed.cleanupOwner; await this.save(claimed); throw error;
      }
      return;
    }
  }
  async stop(id: string): Promise<void> {
    this.path(id);
    const record = (await this.list()).find(record => record.runId === id); if (!record) return;
    if (await ownerAlive(record.owner)) { try { process.kill(record.owner.pid, 'SIGTERM'); } catch (error) { if (!errno(error, 'ESRCH')) throw error; } }
    const deadline = performance.now() + fixedWait(20000);
    while (true) {
      const current = await readJson(this.path(id)) as RunRecord | undefined;
      if (!current || current.owner.pid !== record.owner.pid || current.owner.startedAt !== record.owner.startedAt) return;
      if (!await ownerAlive(parseOwner(current.owner))) { await this.recover(current); return; }
      if (performance.now() >= deadline) throw new Failure('in_use', 'The run did not stop within twenty seconds.');
      await new Promise(resolve => setTimeout(resolve, fixedWait(50)));
    }
  }
  async reserve(request: RunRequest, conversation: string, continuedConversation?: string): Promise<RunRecord> {
    const owner = await processOwner(process.pid);
    if (!owner) throw new Failure('internal', 'Cannot identify the run owner.');
    const record = { runId: request.runId, subscriptionId: request.subscriptionId, owner,
      startedAt: new Date().toISOString(), ...(request.labels ? { labels: request.labels } : {}), conversation, ...(continuedConversation ? { continuedConversation } : {}), ...(request.chatSessionId ? { chatKey: chatKey(request.chatSessionId) } : {}) };
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
