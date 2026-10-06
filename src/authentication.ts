import { readFile, writeFile, readdir, rm, unlink } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { Failure, safeFailure, safeText } from './errors.js';
import { childCommand, startChild } from './child.js';
import type { ManagedChild } from './child.js';
import { Secrets, keychainService, validateKey } from './secrets.js';
import { gateway, normalizeQuota } from './gateway.js';
import { isObject } from './settings.js';
import { inspectTool, requireTool } from './tool.js';
import { fixedWait, ownerAlive, processOwner, parseOwner, privateDirectory, readJson, writeJson } from './state.js';
import type { Owner, ProcessEnv } from './types.js';
import { Subscriptions, familySettings, tuningVariables } from './subscriptions.js';
import { Runs } from './runs.js';
import type { Subscription, SubscriptionRecord, UsageReport } from './subscriptions.js';

interface Progress { owner: Owner; expiresAt: string; operation?: 'sign_in' | 'sign_out' | 'remove'; stopReason?: 'replaced' | 'signed_out'; tool?: Owner }
export interface ModelCatalog {
  source: 'tool' | 'vendor' | 'builtin'; fetchedAt: string;
  models: Array<{ id: string; label: string; description?: string; efforts?: string[]; isDefault?: boolean }>;
}
const sleep = async (ms: number): Promise<void> => new Promise(resolve => setTimeout(resolve, ms));
const sameOwner = (a: Owner, b: Owner): boolean => a.pid === b.pid && a.startedAt === b.startedAt;
const quotaPath = '/api/monitor/usage/quota/limit';
const modelPath = '/api/paas/v4/models';

export class Authentication {
  private constructor(readonly subscriptions: Subscriptions, readonly env: ProcessEnv, readonly signal: AbortSignal, readonly secrets: Secrets) {}
  static async create(subscriptions: Subscriptions, env: ProcessEnv, signal: AbortSignal): Promise<Authentication> {
    const home = env.HOME!;
    return new Authentication(subscriptions, env, signal, new Secrets(await keychainService(subscriptions.state.directory), home, env, signal));
  }
  private path(id: string): string { return join(this.subscriptions.state.subscription(id), 'sign-in.json'); }
  private temporary(id: string): string { return join(this.subscriptions.state.directory, 'tmp', `sign-in-${id}`); }
  private async progress(id: string): Promise<Progress | undefined> {
    const value = await readJson(this.path(id));
    if (value === undefined) return undefined;
    if (!isObject(value) || typeof value.expiresAt !== 'string' || !Number.isFinite(Date.parse(value.expiresAt)) ||
      value.operation !== undefined && !['sign_in', 'sign_out', 'remove'].includes(value.operation as string) ||
      value.stopReason !== undefined && !['replaced', 'signed_out'].includes(value.stopReason as string))
      throw new Failure('internal', 'Malformed sign-in record.');
    parseOwner(value.owner); if (value.tool !== undefined) parseOwner(value.tool);
    return value as unknown as Progress;
  }
  private async endOwner(owner: Owner): Promise<void> {
    if (await ownerAlive(owner)) { try { process.kill(-owner.pid, 'SIGKILL'); } catch { try { process.kill(owner.pid, 'SIGKILL'); } catch { /* gone */ } } }
  }
  async cleanProgress(id: string): Promise<void> {
    const progress = await this.progress(id);
    if (!progress || await ownerAlive(progress.owner)) return;
    if (progress.tool) await this.endOwner(progress.tool);
    await this.subscriptions.state.withSubscriptionLock(id, async () => {
      const current = await this.progress(id);
      if (current && sameOwner(current.owner, progress.owner)) {
        await rm(this.temporary(id), { recursive: true, force: true });
        await unlink(this.path(id));
      }
    });
  }
  async cleanAllProgress(): Promise<void> {
    const entries = await readdir(join(this.subscriptions.state.directory, 'subscriptions'), { withFileTypes: true }).catch(error => {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') return []; throw error;
    });
    for (const entry of entries) if (entry.isDirectory()) await this.cleanProgress(entry.name);
  }
  private async reserve(id: string, operation: NonNullable<Progress['operation']>): Promise<Progress> {
    await this.cleanProgress(id);
    const owner = await processOwner(process.pid);
    if (!owner) throw new Failure('internal', 'Cannot identify the Driver process.');
    const deadline = performance.now() + fixedWait(5000);
    while (true) {
      this.signal.throwIfAborted();
      const existing = await this.subscriptions.state.withSubscriptionLock(id, async () => {
        await this.subscriptions.record(id);
        const current = await this.progress(id);
        if (current) {
          if (current.operation && current.operation !== 'sign_in') throw new Failure('in_use', 'The Subscription is being signed out or removed.');
          current.stopReason = operation === 'sign_in' ? 'replaced' : 'signed_out';
          await writeJson(this.path(id), current);
          return current;
        }
        const progress: Progress = { owner, operation, expiresAt: new Date(Date.now() + fixedWait(300000)).toISOString() };
        await writeJson(this.path(id), progress);
        return undefined;
      });
      if (!existing) return (await this.progress(id))!;
      if (await ownerAlive(existing.owner)) { try { process.kill(existing.owner.pid, 'SIGTERM'); } catch { /* gone */ } }
      else await this.cleanProgress(id);
      if (performance.now() >= deadline) throw new Failure('in_use', 'The previous sign-in did not stop within five seconds.');
      await sleep(fixedWait(50));
    }
  }
  private async release(id: string, owner: Owner): Promise<void> {
    await this.subscriptions.state.withSubscriptionLock(id, async () => {
      const current = await this.progress(id);
      if (current && sameOwner(current.owner, owner)) {
        await rm(this.temporary(id), { recursive: true, force: true });
        await unlink(this.path(id));
      }
    });
  }
  private async save(id: string, change: (record: SubscriptionRecord) => SubscriptionRecord,
    revision?: string, owner?: Owner): Promise<void> {
    await this.subscriptions.state.withSubscriptionLock(id, async () => {
      const record = await this.subscriptions.record(id);
      if (owner) {
        const progress = await this.progress(id);
        if (!progress || !sameOwner(progress.owner, owner) || progress.stopReason) throw new Failure('in_use', 'Another operation replaced this sign-in.');
      }
      if (revision !== undefined && record.updatedAt !== revision) throw new Failure('in_use', 'The Subscription changed while the vendor was answering. Retry the operation.');
      await writeJson(join(this.subscriptions.state.subscription(id), 'record.json'), { ...change(record), updatedAt: new Date().toISOString() });
    });
  }
  private unsigned(record: SubscriptionRecord): SubscriptionRecord {
    const { account: _account, problem: _problem, lastUsage: _usage, checkedAt: _checked, ...rest } = record;
    return { ...rest, state: 'signed_out' };
  }
  private async tool(): Promise<string> {
    const tool = await inspectTool(this.subscriptions.state, this.subscriptions.settings.toolPath, this.env, this.signal);
    requireTool(tool); return tool.path!;
  }
  private async key(id: string): Promise<string> {
    const key = await this.secrets.read(id, this.subscriptions.settings.subscriptions?.[id]?.secret);
    if (key === undefined) throw new Failure('credential_rejected', 'The Subscription has no key.', { remedy: 'Replace the key.' });
    validateKey(key); return key;
  }
  private base(subscription: Subscription): string { return subscription.settings.baseUrl as string ?? 'https://api.z.ai/api/anthropic'; }
  private async status(id: string, signal = this.signal): Promise<{ accepted: boolean; account?: Subscription['account'] }> {
    const env = await this.subscriptions.environment(id, this.env);
    const result = await childCommand(await this.tool(), ['auth', 'status', '--json'], env, env.CLAUDE_CONFIG_DIR!, 20000, signal);
    let value: unknown;
    try { value = JSON.parse(result.stdout); } catch { throw new Failure('tool_failed', 'claude returned an unreadable account status.'); }
    if (!isObject(value) || typeof value.loggedIn !== 'boolean' || (result.code !== 0 && value.loggedIn))
      throw new Failure('tool_failed', 'claude could not report its account status.');
    const account: NonNullable<Subscription['account']> = {};
    for (const [target, source] of [['name', 'name'], ['email', 'email'], ['organization', 'orgName'], ['plan', 'subscriptionType']] as const)
      if (typeof value[source] === 'string') account[target] = value[source];
    return { accepted: value.loggedIn, ...(Object.keys(account).length ? { account } : {}) };
  }
  private async rejection(subscription: Subscription, failure: Failure): Promise<void> {
    if (failure.result.code !== 'credential_rejected') return;
    await this.save(subscription.id, record => ({ ...record, state: 'needs_attention', problem: failure.result, checkedAt: new Date().toISOString() }), subscription.updatedAt);
  }
  async check(id: string): Promise<Subscription> {
    await this.cleanProgress(id);
    const subscription = await this.subscriptions.get(id);
    if (subscription.state === 'signed_out') throw new Failure('not_signed_in', 'Sign in to this Subscription first.', { remedy: 'Sign in.' });
    try {
      let account: Subscription['account'];
      if (subscription.family === 'anthropic') {
        const status = await this.status(id);
        if (!status.accepted) throw new Failure('credential_rejected', 'claude reports that the account is not logged in.', { remedy: 'Sign in.' });
        account = status.account;
      } else { const key = await this.key(id); normalizeQuota(await gateway(this.base(subscription), quotaPath, key, 20000, this.signal), new Date(), key); }
      await this.save(id, record => {
        const { problem: _problem, account: _account, ...rest } = record;
        return { ...rest, state: 'signed_in', ...(account ? { account } : {}), checkedAt: new Date().toISOString() };
      }, subscription.updatedAt);
    } catch (error) {
      if (!(error instanceof Failure) || error.result.code !== 'credential_rejected') throw error;
      await this.rejection(subscription, error);
    }
    return this.subscriptions.get(id);
  }
  async usage(id: string): Promise<UsageReport> {
    const subscription = await this.subscriptions.get(id);
    if (subscription.state !== 'signed_in') return { outcome: 'needs_sign_in', message: 'Sign in to this Subscription first.', fetchedAt: new Date().toISOString(), windows: [] };
    if (subscription.family === 'anthropic') return subscription.lastUsage ?? { outcome: 'unsupported', message: 'Usage is available after a run reports it.', fetchedAt: new Date().toISOString(), windows: [] };
    try {
      const key = await this.key(id);
      const report = normalizeQuota(await gateway(this.base(subscription), quotaPath, key, 20000, this.signal), new Date(), key);
      await this.save(id, record => ({ ...record, lastUsage: report }), subscription.updatedAt);
      return report;
    } catch (error) {
      if (error instanceof Failure && error.result.code === 'credential_rejected') {
        await this.rejection(subscription, error);
        return { outcome: 'needs_sign_in', message: error.message, fetchedAt: new Date().toISOString(), windows: [] };
      }
      if (!(error instanceof Failure) || !['vendor_unavailable', 'tool_failed', 'invalid_input'].includes(error.result.code)) throw error;
      return { outcome: 'unavailable', message: error.message, fetchedAt: new Date().toISOString(), windows: [] };
    }
  }
  async models(id: string): Promise<ModelCatalog> {
    const subscription = await this.subscriptions.requireSignedIn(id);
    const fetchedAt = new Date().toISOString();
    if (subscription.family === 'zai') {
      const fallback: ModelCatalog = { source: 'builtin', fetchedAt, models: [...new Set(Object.values(subscription.settings.modelMap as Record<string, string> ?? {}))].map(id => ({ id, label: id })) };
      try {
        const key = await this.key(id);
        const input = await gateway(this.base(subscription), modelPath, key, 30000, this.signal);
        if (!isObject(input) || !Array.isArray(input.data)) throw new Failure('vendor_unavailable', 'Z.AI returned an unreadable model catalog.');
        const models = input.data.filter(isObject).flatMap(item => {
          const model = [item.id, item.model, item.name].find(value => typeof value === 'string' && value);
          return typeof model === 'string' ? [{ id: safeText(model, [key]), label: safeText(typeof item.label === 'string' ? item.label : model, [key]) }] : [];
        });
        if (!models.length) return fallback;
        return { source: 'vendor', fetchedAt, models };
      } catch (error) {
        if (error instanceof Failure && error.result.code === 'credential_rejected') await this.rejection(subscription, error);
        if (!(error instanceof Failure) || !['vendor_unavailable', 'credential_rejected', 'tool_failed', 'invalid_input'].includes(error.result.code)) throw error;
        return fallback;
      }
    }
    const program = await this.tool();
    const env = await this.subscriptions.environment(id, this.env);
    const child = startChild(program, ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--permission-mode', 'bypassPermissions'],
      { ...env, CLAUDE_CODE_DISABLE_AUTO_MEMORY: '1' }, env.CLAUDE_CONFIG_DIR!, 30000, this.signal);
    let answer: Record<string, unknown> | undefined;
    try {
      // Keep input open until initialize answers; EOF can stop the real tool before it answers.
      child.process.stdin.write(JSON.stringify({ type: 'control_request', request_id: 'catalog', request: { subtype: 'initialize' } }) + '\n');
      const lines = createInterface({ input: child.process.stdout });
      for await (const line of lines) {
        let record: unknown; try { record = JSON.parse(line); } catch { continue; }
        if (isObject(record) && record.type === 'control_response' && isObject(record.response) && record.response.request_id === 'catalog') {
          if (record.response.subtype === 'success' && isObject(record.response.response)) answer = record.response.response;
          break;
        }
      }
      lines.close(); child.process.stdin.end();
      if (!answer || !Array.isArray(answer.models)) throw new Failure('tool_failed', 'claude did not return a model catalog.');
      const models = answer.models.filter(isObject).flatMap(item => {
        if (typeof item.value !== 'string' || typeof item.displayName !== 'string') return [];
        return [{ id: item.value, label: item.displayName, ...(typeof item.description === 'string' ? { description: item.description } : {}),
          ...(item.supportsEffort === true && Array.isArray(item.supportedEffortLevels) && item.supportedEffortLevels.every(value => typeof value === 'string') ? { efforts: item.supportedEffortLevels as string[] } : {}),
          ...(item.value === 'default' ? { isDefault: true } : {}) }];
      });
      if (!models.length) throw new Failure('tool_failed', 'claude returned an empty model catalog.');
      return { source: 'tool', fetchedAt, models };
    } catch (error) {
      if (!(error instanceof Failure) || error.result.code !== 'tool_failed') throw error;
      return { source: 'builtin', fetchedAt, models: ['default', 'opus', 'sonnet', 'haiku'].map(id => ({ id, label: id })) };
    } finally { await child.stop(); }
  }
  async signIn(id: string, byCode: boolean, emit: (step: unknown) => void): Promise<number> {
    const subscription = await this.subscriptions.get(id);
    if (byCode && subscription.family !== 'anthropic') throw new Failure('unsupported', 'This family has no code sign-in.');
    const progress = await this.reserve(id, 'sign_in');
    const attempt = new AbortController();
    const signal = AbortSignal.any([this.signal, attempt.signal]);
    const attemptSecrets = new Secrets(this.secrets.service, this.secrets.home, this.env, signal);
    const timer = setTimeout(() => attempt.abort(), Math.max(1, Date.parse(progress.expiresAt) - Date.now()));
    let child: ManagedChild | undefined;
    let succeeded = false;
    try {
      await this.save(id, record => this.unsigned(record), undefined, progress.owner);
      if (subscription.family === 'zai') {
        const source = this.subscriptions.settings.subscriptions?.[id]?.secret;
        let key = await attemptSecrets.read(id, source);
        if (key === undefined) {
          emit({ step: 'store_secret', prompt: 'Store the Z.AI key in the named Keychain item.', keychain: { service: this.secrets.service, account: id }, expiresAt: progress.expiresAt });
          while (key === undefined) { signal.throwIfAborted(); await sleep(fixedWait(250)); key = await attemptSecrets.read(id, source); }
        }
        try { validateKey(key); } catch (error) {
          if (source?.from !== 'command') await attemptSecrets.remove(id);
          else if (error instanceof Failure) throw new Failure('invalid_input', `${error.message} The Driver cannot remove a key from its source command.`);
          throw error;
        }
        let checkedAt: string | undefined;
        try {
          normalizeQuota(await gateway(this.base(subscription), quotaPath, key, 20000, signal), new Date(), key);
          checkedAt = new Date().toISOString();
        } catch (error) {
          if (error instanceof Failure && error.result.code === 'credential_rejected') {
            if (source?.from !== 'command') await attemptSecrets.remove(id);
            throw new Failure('credential_rejected', source?.from === 'command' ? 'Z.AI rejected the key from the source command; the Driver cannot remove that key.' : 'Z.AI rejected the key; it was removed from the Keychain.', { remedy: 'Replace the key.' });
          }
          if (!(error instanceof Failure) || error.result.code !== 'vendor_unavailable') throw error;
        }
        signal.throwIfAborted();
        await this.save(id, record => ({ ...record, state: 'signed_in', ...(checkedAt ? { checkedAt } : {}) }), undefined, progress.owner);
      } else {
        const folder = this.temporary(id); await privateDirectory(folder);
        const address = join(folder, 'address'); const browser = join(folder, 'browser.mjs');
        // Browser receives only the tool's URL. The shebang uses the current Node binary.
        await writeFile(browser, `#!${process.execPath}\nimport {writeFileSync} from 'node:fs';\nwriteFileSync(${JSON.stringify(address)}, process.argv[2], {mode:0o600});\n`, { mode: 0o700 });
        const env = await this.subscriptions.environment(id, this.env);
        let output = '';
        child = startChild(await this.tool(), ['auth', 'login', '--claudeai'], { ...env, BROWSER: browser }, env.CLAUDE_CONFIG_DIR!, fixedWait(300000), signal, text => { output = text; });
        if (child.process.pid) {
          const tool = await processOwner(child.process.pid);
          if (tool) await this.subscriptions.state.withSubscriptionLock(id, async () => {
            const current = await this.progress(id);
            if (current && sameOwner(current.owner, progress.owner)) await writeJson(this.path(id), { ...current, tool });
          });
        }
        let ended = false; void child.done.finally(() => { ended = true; }).catch(() => {});
        let url: string | undefined;
        while (!url && !ended) {
          signal.throwIfAborted();
          if (byCode) url = output.match(/https:\/\/[^\s]+/g)?.find(value => value.includes('platform.claude.com%2Foauth%2Fcode%2Fcallback') || value.includes('platform.claude.com/oauth/code/callback'));
          else url = await readFile(address, 'utf8').catch(() => undefined);
          if (!url) await sleep(fixedWait(50));
        }
        if (url) {
          const host = new URL(url).hostname;
          const allowedHosts = ['claude.com', 'claude.ai', 'platform.claude.com', 'localhost'];
          if (!allowedHosts.includes(host)) throw new Failure('tool_failed', 'claude returned a sign-in address on an unexpected host.');
          emit({ step: 'open_url', url, allowedHosts, expiresAt: progress.expiresAt });
          if (byCode) {
            emit({ step: 'enter_code', prompt: 'Enter the one-time code shown by Claude.', expiresAt: progress.expiresAt });
            const lines = createInterface({ input: process.stdin });
            const cancel = (): void => lines.close(); signal.addEventListener('abort', cancel, { once: true });
            try {
              const first = await lines[Symbol.asyncIterator]().next(); signal.throwIfAborted();
              let input: unknown; try { input = JSON.parse(first.value ?? ''); } catch { throw new Failure('invalid_input', 'Enter the code as one JSON line.'); }
              if (!isObject(input) || typeof input.input !== 'string' || !input.input || /[\r\n]/.test(input.input)) throw new Failure('invalid_input', 'Enter one nonempty code.');
              child.process.stdin.write(input.input + '\n');
            } finally { signal.removeEventListener('abort', cancel); lines.close(); }
          }
        }
        const result = await child.done;
        if (result.code !== 0) throw new Failure('credential_rejected', 'Claude sign-in failed. Try signing in again.', { remedy: 'Sign in.' });
        const status = await this.status(id, signal);
        if (!status.accepted) throw new Failure('credential_rejected', 'claude did not confirm the login.', { remedy: 'Sign in.' });
        signal.throwIfAborted();
        await this.save(id, record => ({ ...record, state: 'signed_in', ...(status.account ? { account: status.account } : {}), checkedAt: new Date().toISOString() }), undefined, progress.owner);
      }
      succeeded = true;
      await this.release(id, progress.owner);
      emit({ step: 'done', subscription: await this.subscriptions.get(id) }); return 0;
    } catch (error) {
      const current = await this.progress(id);
      const reason = current?.stopReason;
      const failure = reason === 'replaced' ? new Failure('in_use', 'Another sign-in replaced this attempt.') : reason === 'signed_out' ? new Failure('in_use', 'The Subscription was signed out during this attempt.') :
        attempt.signal.aborted ? new Failure('limit_reached', 'Sign-in timed out after five minutes.') : this.signal.aborted ? new Failure('tool_failed', 'Sign-in was cancelled.') : error;
      emit({ step: 'failed', error: safeFailure(failure) });
      return this.signal.aborted && !reason ? 2 : 1;
    } finally {
      clearTimeout(timer); if (child) await child.stop();
      if (!succeeded) await this.subscriptions.state.withSubscriptionLock(id, async () => {
        const current = await this.progress(id);
        if (current && sameOwner(current.owner, progress.owner)) {
          const record = await this.subscriptions.record(id);
          await writeJson(join(this.subscriptions.state.subscription(id), 'record.json'), { ...this.unsigned(record), updatedAt: new Date().toISOString() });
        }
      });
      if (!succeeded && subscription.family === 'zai' && (attempt.signal.aborted || this.signal.aborted) && this.subscriptions.settings.subscriptions?.[id]?.secret?.from !== 'command') {
        await new Secrets(this.secrets.service, this.secrets.home, this.env).remove(id);
      }
      if (!succeeded && subscription.family === 'anthropic') {
        // Logout is tool-owned; the Driver never reads its credential.
        const env = await this.subscriptions.environment(id, this.env);
        const cleanupTool = await inspectTool(this.subscriptions.state, this.subscriptions.settings.toolPath, this.env);
        if (cleanupTool.path && cleanupTool.supported) await childCommand(cleanupTool.path, ['auth', 'logout'], env, env.CLAUDE_CONFIG_DIR!, 20000).catch(() => {});
      }
      await this.release(id, progress.owner);
    }
  }
  private async runs(id: string, force: boolean): Promise<void> {
    const runs = new Runs(this.subscriptions.state);
    for (const record of (await runs.list()).filter(record => record.subscriptionId === id)) {
      if (!force) throw new Failure('in_use', 'A run is using the Subscription. Stop it first or use --force.');
      await runs.stop(record.runId);
    }
  }
  async signOut(id: string, force: boolean, remove = false): Promise<Subscription | undefined> {
    const subscription = await this.subscriptions.get(id);
    await this.runs(id, force);
    const progress = await this.reserve(id, remove ? 'remove' : 'sign_out');
    try {
      // Invalidate pending checks before touching the vendor's credential.
      await this.save(id, record => this.unsigned(record), undefined, progress.owner);
      if (subscription.family === 'anthropic' && subscription.state !== 'signed_out') {
        const env = await this.subscriptions.environment(id, this.env);
        const result = await childCommand(await this.tool(), ['auth', 'logout'], env, env.CLAUDE_CONFIG_DIR!, 20000, this.signal);
        if (result.code !== 0) throw new Failure('tool_failed', 'claude could not sign the account out.');
      } else if (subscription.family === 'zai' && this.subscriptions.settings.subscriptions?.[id]?.secret?.from !== 'command') await this.secrets.remove(id);
      if (remove) {
        const folder = this.subscriptions.state.subscription(id);
        await rm(join(folder, 'home'), { recursive: true, force: true });
        await rm(join(folder, 'chats'), { recursive: true, force: true });
        await rm(join(folder, 'record.json'), { force: true });
      }
    } finally { await this.release(id, progress.owner); }
    return remove ? undefined : this.subscriptions.get(id);
  }
  async import(id: string, file: string): Promise<Subscription> {
    const subscription = await this.subscriptions.get(id);
    if (subscription.family !== 'zai') throw new Failure('invalid_input', 'Import a gateway setup into a Z.AI Subscription.');
    if (this.subscriptions.settings.subscriptions?.[id]?.secret?.from === 'command') throw new Failure('invalid_input', 'This Subscription reads its key from a command. Choose a Keychain source before importing.');
    let input: unknown;
    try { input = JSON.parse(await readFile(file, 'utf8')); } catch { throw new Failure('invalid_input', 'The import file could not be read as JSON.'); }
    if (!isObject(input) || !isObject(input.env)) throw new Failure('invalid_input', 'The import file must contain an env object.');
    const env = input.env;
    if (typeof env.ANTHROPIC_AUTH_TOKEN !== 'string') throw new Failure('invalid_input', 'The import file has no Z.AI key.');
    const key = env.ANTHROPIC_AUTH_TOKEN; validateKey(key);
    const imported: Record<string, unknown> = {};
    if (env.ANTHROPIC_BASE_URL !== undefined) imported.baseUrl = env.ANTHROPIC_BASE_URL;
    const modelMap: Record<string, unknown> = {}, toolEnvironment: Record<string, unknown> = {};
    for (const name of ['haiku', 'sonnet', 'opus']) if (env[`ANTHROPIC_DEFAULT_${name.toUpperCase()}_MODEL`] !== undefined) modelMap[name] = env[`ANTHROPIC_DEFAULT_${name.toUpperCase()}_MODEL`];
    for (const name of tuningVariables) if (env[name] !== undefined) toolEnvironment[name] = env[name];
    imported.modelMap = modelMap; imported.toolEnvironment = toolEnvironment;
    const settings = familySettings('zai', imported, 'import.', []);
    const progress = await this.reserve(id, 'sign_out');
    try {
      let raw: unknown;
      try { raw = JSON.parse(await readFile(this.subscriptions.settingsFile, 'utf8')); } catch (error) {
        if (error instanceof Error && 'code' in error && error.code === 'ENOENT') raw = {}; else throw new Failure('invalid_input', 'The Driver settings could not be read for import.');
      }
      if (!isObject(raw)) throw new Failure('invalid_input', 'The Driver settings must be an object.');
      const records = isObject(raw.subscriptions) ? raw.subscriptions : {};
      const old = isObject(records[id]) ? records[id] : {};
      await this.secrets.store(id, key);
      await writeJson(this.subscriptions.settingsFile, { ...raw, subscriptions: { ...records, [id]: { ...old, settings: { ...subscription.settings, ...settings } } } });
      await this.save(id, record => this.unsigned(record), undefined, progress.owner);
    } finally { await this.release(id, progress.owner); }
    return { ...await this.subscriptions.get(id), settings: { ...subscription.settings, ...settings } };
  }
}
