import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { Failure, errno } from './errors.js';
export interface Notice { code: string; message: string }
export type SecretSource = { from: 'keychain' } | { from: 'command'; command: string[] };
export interface SubscriptionSettings {
  defaultModel?: string;
  defaultEffort?: string;
  settings?: Record<string, unknown>;
  secret?: SecretSource;
}
export interface DriverSettings {
  toolPath?: string;
  defaultSubscription?: string;
  subscriptions?: Record<string, SubscriptionSettings>;
}
export const settingsPath = (home: string): string => join(home, '.arbol', 'arbol-claude-driver.json');
export function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}
export async function readSettings(file: string): Promise<{ settings: DriverSettings; notices: Notice[] }> {
  let text: string;
  try { text = await readFile(file, 'utf8'); }
  catch (error) {
    if (errno(error, 'ENOENT')) return { settings: {}, notices: [] };
    throw new Failure('invalid_input', `Cannot read settings file ${file}.`);
  }
  let input: unknown;
  try { input = JSON.parse(text); }
  catch { throw new Failure('invalid_input', `Settings file ${file} is not valid JSON.`); }
  const notices: Notice[] = [];
  const bad = (field: string): never => { throw new Failure('invalid_input', `Settings file ${file}: invalid field ${field}.`); };
  const object = (value: unknown, field: string): Record<string, unknown> => isObject(value) ? value : bad(field);
  const unknownKeys = (value: Record<string, unknown>, known: string[], prefix: string): void => {
    for (const key of Object.keys(value)) if (!known.includes(key)) {
      notices.push({ code: 'unknown_setting', message: `Ignoring unknown setting ${prefix}${key} in ${file}.` });
    }
  };
  const optionalString = (value: Record<string, unknown>, key: string, prefix = ''): string | undefined => {
    if (!(key in value)) return undefined;
    return typeof value[key] === 'string' && value[key].length > 0 ? value[key] : bad(prefix + key);
  };
  const root = object(input, '(root)');
  unknownKeys(root, ['toolPath', 'defaultSubscription', 'subscriptions'], '');
  const settings: DriverSettings = {};
  const toolPath = optionalString(root, 'toolPath');
  const defaultSubscription = optionalString(root, 'defaultSubscription');
  if (toolPath !== undefined) settings.toolPath = toolPath;
  if (defaultSubscription !== undefined) settings.defaultSubscription = defaultSubscription;
  if ('subscriptions' in root) {
    const subscriptions = object(root.subscriptions, 'subscriptions');
    settings.subscriptions = Object.create(null) as Record<string, SubscriptionSettings>;
    for (const [id, value] of Object.entries(subscriptions)) {
      const prefix = `subscriptions.${id}.`;
      const item = object(value, `subscriptions.${id}`);
      unknownKeys(item, ['defaultModel', 'defaultEffort', 'settings', 'secret'], prefix);
      const clean: SubscriptionSettings = {};
      const model = optionalString(item, 'defaultModel', prefix);
      const effort = optionalString(item, 'defaultEffort', prefix);
      if (model !== undefined) clean.defaultModel = model;
      if (effort !== undefined) clean.defaultEffort = effort;
      if ('settings' in item) clean.settings = object(item.settings, prefix + 'settings');
      if ('secret' in item) {
        const secret = object(item.secret, prefix + 'secret');
        if (secret.from === 'keychain') {
          unknownKeys(secret, ['from'], prefix + 'secret.');
          clean.secret = { from: 'keychain' };
        } else if (secret.from === 'command') {
          unknownKeys(secret, ['from', 'command'], prefix + 'secret.');
          if (!Array.isArray(secret.command) || secret.command.length === 0 ||
            !secret.command.every((arg: unknown) => typeof arg === 'string') || !secret.command[0]) bad(prefix + 'secret.command');
          clean.secret = { from: 'command', command: secret.command as string[] };
        } else bad(prefix + 'secret.from');
      }
      settings.subscriptions[id] = clean;
    }
  }
  return { settings, notices };
}
