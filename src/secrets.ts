import { createHash } from 'node:crypto';
import { realpath } from 'node:fs/promises';
import { resolve } from 'node:path';
import { userInfo } from 'node:os';
import { Failure } from './errors.js';
import { childCommand } from './child.js';
import { cleanEnvironment } from './environment.js';
import { defaultStateDirectory } from './state.js';
import type { SecretSource } from './settings.js';
import type { ProcessEnv } from './types.js';

export const KEYCHAIN_SERVICE = 'com.sylvan-stack.arbol.claude-driver';
export async function keychainService(directory: string, home = userInfo().homedir): Promise<string> {
  const path = await realpath(directory);
  const normal = await realpath(defaultStateDirectory(home)).catch(() => resolve(defaultStateDirectory(home)));
  return path === normal ? KEYCHAIN_SERVICE : `${KEYCHAIN_SERVICE}.${createHash('sha256').update(path).digest('hex').slice(0, 12)}`;
}
export function validateKey(key: string): void {
  const bad = [...key].filter(char => !/^[\x21-\x7e]$/.test(char)).slice(0, 3);
  if (!key || bad.length) throw new Failure('invalid_input', !key ? 'The key is empty.' :
    `The key contains invalid characters: ${bad.map(char => `${JSON.stringify(char)} (U+${char.codePointAt(0)!.toString(16).toUpperCase().padStart(4, '0')})`).join(', ')}.`);
}
const lineEnding = (value: string): string => value.replace(/\r?\n$/, '');
const quote = (value: string): string => '"' + value.replaceAll('\\', '\\\\').replaceAll('"', '\\"') + '"';

export class Secrets {
  readonly env: ProcessEnv;
  constructor(readonly service: string, readonly home: string, inherited: ProcessEnv,
    readonly signal?: AbortSignal, private readonly security = '/usr/bin/security') {
    this.env = cleanEnvironment(inherited, home);
  }
  private async item(args: string[], input?: string) {
    return childCommand(this.security, args, { ...this.env, HOME: userInfo().homedir }, userInfo().homedir, 20000, this.signal, input, args.includes('-g'));
  }
  async read(id: string, source?: SecretSource): Promise<string | undefined> {
    if (source?.from === 'command') {
      const result = await childCommand(source.command[0]!, source.command.slice(1), this.env, this.home, 20000, this.signal);
      if (result.code !== 0) throw new Failure('tool_failed', 'The secret-source command failed. Its output was withheld.');
      return lineEnding(result.stdout);
    }
    const result = await this.item(['find-generic-password', '-s', this.service, '-a', id, '-w']);
    if (result.code === 44) return undefined;
    if (result.code !== 0) throw new Failure('tool_failed', 'The Keychain item could not be read.');
    const value = lineEnding(result.stdout);
    // security prints binary/non-ASCII values as hex. Plain ASCII keys may also be all hex.
    if (/^(?:[0-9a-f]{2})+$/i.test(value)) {
      const typed = await this.item(['find-generic-password', '-s', this.service, '-a', id, '-g']);
      if (typed.code !== 0) throw new Failure('tool_failed', 'The Keychain item could not be read.');
      if (/^password: 0x[0-9a-f]+/im.test(typed.stderr ?? '')) return Buffer.from(value, 'hex').toString('utf8');
    }
    return value;
  }
  async remove(id: string): Promise<void> {
    const result = await this.item(['delete-generic-password', '-s', this.service, '-a', id]);
    if (![0, 44].includes(result.code)) throw new Failure('tool_failed', 'The Keychain item could not be removed.');
  }
  async store(id: string, key: string): Promise<void> {
    validateKey(key);
    const result = await this.item(['-i'], `add-generic-password -U -s ${quote(this.service)} -a ${quote(id)} -w ${quote(key)}\n`);
    if (result.code !== 0 || await this.read(id) !== key) throw new Failure('tool_failed', 'The imported key could not be stored in the Keychain.');
  }
}
