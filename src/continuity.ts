import { randomUUID } from 'node:crypto';
import { readdir, stat, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure, errno } from './errors.js';
import { isObject } from './settings.js';
import { atomicWrite, readJson, writeJson } from './state.js';
import type { State } from './state.js';
import { chatKey, digest, messageDigest, sessionLines } from './history.js';
import type { RunRequest } from './run-request.js';
import type { RunRecord, Runs } from './runs.js';
export type RebuildReason = 'no_chat_session' | 'first_run' | 'history_differs' | 'working_folder_changed' | 'conversation_gone' | 'in_use' | 'resume_refused';
export interface Continuity {
  continuity: 'fresh' | 'resumed' | 'rebuilt'; rebuildReason?: RebuildReason;
  conversation: string; resume: boolean; textForm: boolean; continuedConversation?: string; syntheticFile?: string;
}
export interface ChatEntry {
  chatSessionId: string; conversation: string; workingDirectory: string; messages: number; digest: string; systemPromptDigest: string; writtenAt: string;
}
export const encodeFolder = (cwd: string): string => cwd.replace(/[^a-zA-Z0-9]/g, '-');
export async function sessionFile(home: string, conversation: string): Promise<string | undefined> {
  const projects = join(home, 'projects');
  const folders = await readdir(projects, { withFileTypes: true }).catch(error => { if (errno(error, 'ENOENT')) return []; throw error; });
  for (const folder of folders) if (folder.isDirectory()) {
    const file = join(projects, folder.name, conversation + '.jsonl');
    if (await stat(file).then(info => info.isFile()).catch(error => { if (errno(error, 'ENOENT')) return false; throw error; })) return file;
  }
  return undefined;
}
export class Chats {
  constructor(readonly state: State) {}
  path(subscription: string, key: string): string { return join(this.state.subscription(subscription), 'chats', key + '.json'); }
  async entry(subscription: string, key: string): Promise<ChatEntry | undefined> {
    const value = await readJson(this.path(subscription, key)); if (value === undefined) return undefined;
    if (!isObject(value) || typeof value.chatSessionId !== 'string' || !/^[a-f0-9-]{36}$/i.test(String(value.conversation)) ||
      typeof value.workingDirectory !== 'string' || !Number.isInteger(value.messages) || (value.messages as number) < 0 ||
      !/^[a-f0-9]{64}$/.test(String(value.digest)) || !/^[a-f0-9]{64}$/.test(String(value.systemPromptDigest)) || typeof value.writtenAt !== 'string') {
      throw new Failure('internal', 'Malformed chat map entry.');
    }
    return value as unknown as ChatEntry;
  }
  async reserve(request: RunRequest, runs: Runs, home: string): Promise<{ plan: Continuity; record: RunRecord }> {
    return this.state.withSubscriptionLock(request.subscriptionId, async () => {
      const active = await runs.list();
      const plan: Continuity = { continuity: request.history.length ? 'rebuilt' : 'fresh', conversation: randomUUID(), resume: false, textForm: false };
      if (request.history.length) {
        let reason: RebuildReason | undefined;
        const key = request.chatSessionId && request.workingDirectory !== undefined ? chatKey(request.chatSessionId) : undefined;
        const entry = key ? await this.entry(request.subscriptionId, key) : undefined;
        const fingerprint = messageDigest(request.history);
        if (!key) reason = 'no_chat_session';
        else if (!entry) reason = 'first_run';
        else if (entry.messages !== fingerprint.messages || entry.digest !== fingerprint.digest) reason = 'history_differs';
        else if (entry.workingDirectory !== request.workingDirectory) reason = 'working_folder_changed';
        else if (!await sessionFile(home, entry.conversation)) reason = 'conversation_gone';
        else if (active.some(run => run.subscriptionId === request.subscriptionId && run.conversation === entry.conversation)) reason = 'in_use';
        if (reason) plan.rebuildReason = reason;
        else { plan.continuity = 'resumed'; plan.resume = true; plan.conversation = entry!.conversation; plan.continuedConversation = entry!.conversation; }
      }
      const record = await runs.reserve(request, plan.conversation);
      return { plan, record };
    });
  }
  async prepare(plan: Continuity, request: RunRequest, home: string, cwd: string): Promise<void> {
    if (plan.continuity !== 'rebuilt') return;
    let folder = encodeFolder(cwd);
    if (folder.length > 200) {
      const candidates = await readdir(join(home, 'projects'), { withFileTypes: true }).catch(error => { if (errno(error, 'ENOENT')) return []; throw error; });
      const existing = candidates.filter(item => item.isDirectory() && item.name.startsWith(folder.slice(0, 200))).sort((a, b) => a.name.localeCompare(b.name))[0];
      if (!existing) { plan.textForm = true; return; }
      folder = existing.name;
    }
    const file = join(home, 'projects', folder, plan.conversation + '.jsonl');
    try { await atomicWrite(file, sessionLines(request.history, plan.conversation, cwd).map(line => JSON.stringify(line)).join('\n') + '\n'); }
    catch { plan.textForm = true; return; }
    plan.syntheticFile = file; plan.resume = true;
  }
  async settle(request: RunRequest, plan: Continuity, fingerprint: { messages: number; digest: string }, completed: boolean, home: string): Promise<void> {
    if (!request.chatSessionId || request.workingDirectory === undefined) return;
    const key = chatKey(request.chatSessionId);
    const saved = completed && await sessionFile(home, plan.conversation);
    await this.state.withSubscriptionLock(request.subscriptionId, async () => {
      if (saved) await writeJson(this.path(request.subscriptionId, key), { chatSessionId: request.chatSessionId,
        conversation: plan.conversation, workingDirectory: request.workingDirectory, ...fingerprint,
        systemPromptDigest: digest(request.systemPrompt ?? ''), writtenAt: new Date().toISOString() });
      else if (plan.continuedConversation && (await this.entry(request.subscriptionId, key))?.conversation === plan.continuedConversation) {
        await rm(this.path(request.subscriptionId, key), { force: true });
      }
    });
  }
}
