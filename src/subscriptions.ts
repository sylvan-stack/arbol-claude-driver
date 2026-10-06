import { readdir, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure, errno } from './errors.js';
import type { DriverError } from './errors.js';
import { isObject } from './settings.js';
import type { DriverSettings, Notice } from './settings.js';
import { ownerAlive, parseOwner, privateDirectory, readJson, writeJson } from './state.js';
import type { State } from './state.js';
import { cleanEnvironment } from './environment.js';
import type { ProcessEnv } from './types.js';

export type FamilyId = 'anthropic' | 'zai';
export type AuthState = 'signed_out' | 'signing_in' | 'signed_in' | 'needs_attention';
export interface UsageReport {
  outcome: 'ok' | 'needs_sign_in' | 'unavailable' | 'unsupported';
  fetchedAt: string;
  message?: string;
  plan?: string;
  windows: Array<{ kind: 'session' | 'weekly' | 'other'; label: string; usedPercent?: number;
    resetsAt?: string; windowMinutes?: number; limitReached?: boolean }>;
  detailsUrl?: string;
  extra?: Record<string, unknown>;
}
export interface Subscription {
  id: string;
  family: FamilyId;
  label: string;
  auth: 'vendor_login' | 'key';
  state: AuthState;
  account?: { name?: string; email?: string; organization?: string; plan?: string };
  problem?: DriverError;
  settings: Record<string, unknown>;
  lastUsage?: UsageReport;
  createdAt: string;
  updatedAt: string;
  checkedAt?: string;
}
export type SubscriptionRecord = Omit<Subscription, 'settings' | 'state'> & { state: Exclude<AuthState, 'signing_in'> };
export const tuningVariables = ['API_TIMEOUT_MS', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] as const;
const modelNames = ['haiku', 'sonnet', 'opus'] as const;
export const families = [
  { id: 'anthropic', label: 'Anthropic', auth: 'vendor_login', settings: { type: 'object', properties: {}, additionalProperties: false } },
  { id: 'zai', label: 'Z.AI', auth: 'key', settings: { type: 'object', additionalProperties: false, properties: {
    baseUrl: { type: 'string', format: 'uri', pattern: '^https?://', default: 'https://api.z.ai/api/anthropic' },
    modelMap: { type: 'object', additionalProperties: false, default: {}, properties: Object.fromEntries(modelNames.map(name => [name, { type: 'string', minLength: 1 }])) },
    toolEnvironment: { type: 'object', additionalProperties: false, default: {}, properties: Object.fromEntries(tuningVariables.map(name => [name, { type: 'string' }])) },
  } } },
];
export function familyId(value: unknown): FamilyId {
  if (value !== 'anthropic' && value !== 'zai') throw new Failure('invalid_input', 'Choose the Subscription family anthropic or zai.');
  return value;
}

export function familySettings(family: FamilyId, input: Record<string, unknown>, prefix: string, notices: Notice[]): Record<string, unknown> {
  const fail = (field: string): never => { throw new Failure('invalid_input', `Invalid setting ${prefix}${field}.`); };
  const unknown = (field: string): void => { notices.push({ code: 'unknown_setting', message: `Ignoring unknown setting ${prefix}${field}.` }); };
  if (family === 'anthropic') { for (const field of Object.keys(input)) unknown(field); return {}; }
  for (const field of Object.keys(input)) if (!['baseUrl', 'modelMap', 'toolEnvironment'].includes(field)) unknown(field);
  const settings: Record<string, unknown> = {};
  if ('baseUrl' in input) {
    if (typeof input.baseUrl !== 'string') fail('baseUrl');
    try {
      const url = new URL(input.baseUrl as string);
      if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) fail('baseUrl');
    } catch { fail('baseUrl'); }
    settings.baseUrl = input.baseUrl;
  }
  for (const field of ['modelMap', 'toolEnvironment']) {
    if (!(field in input)) continue;
    const value = input[field];
    if (!isObject(value)) fail(field);
    const allowed: readonly string[] = field === 'modelMap' ? modelNames : tuningVariables;
    const result: Record<string, string> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      // Tuning names are an explicit allow-list: an account selector is a fault.
      if (!allowed.includes(key)) {
        if (field === 'toolEnvironment') fail(`${field}.${key}`);
        unknown(`${field}.${key}`); continue;
      }
      if (typeof item !== 'string' || (field === 'modelMap' && !item)) fail(`${field}.${key}`);
      result[key] = item as string;
    }
    settings[field] = result;
  }
  return settings;
}

function parseRecord(value: unknown, id: string): SubscriptionRecord {
  const bad = (): never => { throw new Failure('internal', `Malformed Subscription record for ${id}.`); };
  if (!isObject(value) || value.id !== id || !['anthropic', 'zai'].includes(value.family as string) ||
    typeof value.label !== 'string' || !value.label || !['signed_out', 'signed_in', 'needs_attention'].includes(value.state as string) ||
    value.auth !== (value.family === 'anthropic' ? 'vendor_login' : 'key') ||
    !['createdAt', 'updatedAt'].every(field => typeof value[field] === 'string' && Number.isFinite(Date.parse(value[field] as string)))) bad();
  const record = value as unknown as SubscriptionRecord;
  const allowed = ['id', 'family', 'label', 'auth', 'state', 'account', 'problem', 'lastUsage', 'createdAt', 'updatedAt', 'checkedAt'];
  if (Object.keys(value as object).some(key => !allowed.includes(key))) bad();
  if ((record.state === 'needs_attention') !== (record.problem !== undefined)) bad();
  if (record.checkedAt !== undefined && (typeof record.checkedAt !== 'string' || !Number.isFinite(Date.parse(record.checkedAt)))) bad();
  if (record.account !== undefined && (!isObject(record.account) || Object.entries(record.account).some(([key, item]) =>
    !['name', 'email', 'organization', 'plan'].includes(key) || typeof item !== 'string'))) bad();
  return record;
}

export class Subscriptions {
  private readonly resolvedSettings = new Map<string, Record<string, unknown>>();
  constructor(readonly state: State, readonly settings: DriverSettings, readonly notices: Notice[], readonly settingsFile: string) {}

  async record(id: string): Promise<SubscriptionRecord> {
    const path = this.state.subscription(id);
    const value = await readJson(join(path, 'record.json'));
    if (value !== undefined) return parseRecord(value, id);
    if (id !== 'anthropic' && id !== 'zai') throw new Failure('not_found', 'Subscription was not found.');
    const createdAt = (await stat(join(this.state.directory, 'format'))).birthtime.toISOString();
    return { id, family: id, label: id === 'anthropic' ? 'Anthropic' : 'Z.AI', auth: id === 'anthropic' ? 'vendor_login' : 'key',
      state: 'signed_out', createdAt, updatedAt: createdAt };
  }

  async get(id: string): Promise<Subscription> {
    const record = await this.record(id);
    let settings = this.resolvedSettings.get(id);
    if (settings === undefined) {
      settings = familySettings(record.family, this.settings.subscriptions?.[id]?.settings ?? {},
        `${this.settingsFile}: subscriptions.${id}.settings.`, this.notices);
      this.resolvedSettings.set(id, settings);
    }
    const progress = await readJson(join(this.state.subscription(id), 'sign-in.json'));
    let signingIn = false;
    if (progress !== undefined) {
      if (!isObject(progress) || typeof progress.expiresAt !== 'string' || !Number.isFinite(Date.parse(progress.expiresAt))) {
        throw new Failure('internal', 'Malformed sign-in record.');
      }
      signingIn = await ownerAlive(parseOwner(progress.owner));
    }
    const subscription: Subscription = { ...record, settings };
    if (signingIn) { subscription.state = 'signing_in'; delete subscription.problem; }
    return subscription;
  }

  async list(): Promise<Subscription[]> {
    const ids = new Set(['anthropic', 'zai']);
    const directory = join(this.state.directory, 'subscriptions');
    try {
      for (const entry of await readdir(directory, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        if (await readJson(join(this.state.subscription(entry.name), 'record.json')) !== undefined) ids.add(entry.name);
      }
    } catch (error) { if (!errno(error, 'ENOENT')) throw error; }
    const result: Subscription[] = [];
    for (const id of ids) result.push(await this.get(id));
    return result;
  }

  async create(family: FamilyId, label: string, explicitId?: string): Promise<Subscription> {
    if (!label) throw new Failure('invalid_input', 'Give the new Subscription a label.');
    for (let number = 2; ; number++) {
      const id = explicitId ?? `${family}-${number}`;
      // Validate before a change; settings stay in the owner's settings file.
      familySettings(family, this.settings.subscriptions?.[id]?.settings ?? {},
        `${this.settingsFile}: subscriptions.${id}.settings.`, []);
      const created = await this.state.withSubscriptionLock(id, async () => {
        if (id === 'anthropic' || id === 'zai' || await readJson(join(this.state.subscription(id), 'record.json')) !== undefined) return false;
        const time = new Date().toISOString();
        await privateDirectory(join(this.state.subscription(id), 'home'));
        await writeJson(join(this.state.subscription(id), 'record.json'), { id, family, label,
          auth: family === 'anthropic' ? 'vendor_login' : 'key', state: 'signed_out', createdAt: time, updatedAt: time });
        return true;
      });
      if (created) return this.get(id);
      if (explicitId !== undefined) throw new Failure('in_use', 'That Subscription identifier already exists.');
    }
  }

  async update(id: string, label?: string): Promise<Subscription> {
    await this.get(id); // Validate existence and family settings before any mutation.
    if (label === undefined) return this.get(id);
    if (!label) throw new Failure('invalid_input', 'Give the Subscription a non-empty label.');
    await this.state.withSubscriptionLock(id, async () => {
      const record = await this.record(id);
      await privateDirectory(join(this.state.subscription(id), 'home'));
      await writeJson(join(this.state.subscription(id), 'record.json'), { ...record, label, updatedAt: new Date().toISOString() });
    });
    return this.get(id);
  }

  async requireSignedIn(id: string): Promise<Subscription> {
    const subscription = await this.get(id);
    if (subscription.state !== 'signed_in') throw new Failure('not_signed_in', 'Sign in to this Subscription first.', { remedy: 'Sign in.' });
    return subscription;
  }

  async environment(id: string, inherited: ProcessEnv, key?: string): Promise<ProcessEnv> {
    const subscription = await this.get(id);
    const home = join(this.state.subscription(id), 'home');
    await privateDirectory(home);
    const env = cleanEnvironment(inherited, home, home);
    if (subscription.family === 'zai') {
      if (!key) throw new Failure('not_signed_in', 'The Subscription has no key.', { remedy: 'Sign in.' });
      env.ANTHROPIC_BASE_URL = subscription.settings.baseUrl as string ?? 'https://api.z.ai/api/anthropic';
      env.ANTHROPIC_AUTH_TOKEN = key;
      const map = subscription.settings.modelMap as Record<string, string> | undefined;
      for (const name of modelNames) if (map?.[name]) env[`ANTHROPIC_DEFAULT_${name.toUpperCase()}_MODEL`] = map[name];
      Object.assign(env, subscription.settings.toolEnvironment);
    }
    return env;
  }
}
