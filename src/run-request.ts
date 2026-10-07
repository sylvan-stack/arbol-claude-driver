import { randomUUID } from 'node:crypto';
import { readFile, realpath, stat } from 'node:fs/promises';
import { isAbsolute } from 'node:path';
import { Failure } from './errors.js';
import type { HistoryItem } from './history.js';
import { isObject } from './settings.js';
import type { DriverSettings } from './settings.js';
import type { Command } from './arguments.js';

export interface Image { mediaType: 'image/png' | 'image/jpeg' | 'image/webp' | 'image/gif'; data: string }
export interface RunRequest {
  runId: string; subscriptionId: string; model?: string | undefined; effort?: string | undefined; systemPrompt?: string | undefined;
  workingDirectory?: string | undefined; additionalDirectories: string[]; history: HistoryItem[];
  input: { role: 'user'; text: string; images?: Image[] }; chatSessionId?: string | undefined;
  tools: 'all' | 'none'; labels?: Record<string, string> | undefined; searchPath: string[];
  limits?: { idleSeconds?: number; totalSeconds?: number } | undefined;
}
export async function readRequest(command: Command, settings: DriverSettings, firstLine?: () => Promise<string>,
  identity?: (runId: string, labels: Record<string, string> | undefined) => void): Promise<RunRequest> {
  const bad = (message: string): never => { throw new Failure('invalid_input', message); };
  let value: Record<string, unknown> = {};
  const file = command.options.request;
  if (typeof file === 'string') {
    let input: unknown;
    try { input = JSON.parse(file === '-' ? await firstLine!() : await readFile(file, 'utf8')); }
    catch { bad('The request could not be read as JSON.'); }
    if (!isObject(input)) bad('The request must be a JSON object.');
    value = { ...input as Record<string, unknown> };
  }
  for (const [option, field] of Object.entries({ subscription: 'subscriptionId', model: 'model', effort: 'effort',
    'service-tier': 'serviceTier', cwd: 'workingDirectory', tools: 'tools', 'chat-session': 'chatSessionId', 'run-id': 'runId' })) {
    if (command.options[option] !== undefined) value[field] = command.options[option];
  }
  if (command.options['system-prompt-file'] !== undefined) {
    try { value.systemPrompt = await readFile(command.options['system-prompt-file'] as string, 'utf8'); }
    catch { bad('The system prompt file could not be read.'); }
  }
  if (command.positionals.length) value.input = { ...(isObject(value.input) ? value.input : {}), role: 'user', text: command.positionals[0] };
  if (command.options['add-dir']) value.additionalDirectories = command.options['add-dir'];
  if (command.options.label) {
    const labels: Record<string, unknown> = { ...(isObject(value.labels) ? value.labels : {}) };
    for (const item of command.options.label as string[]) {
      const equal = item.indexOf('=');
      if (equal <= 0) bad('A label must be key=value.');
      Object.defineProperty(labels, item.slice(0, equal), { value: item.slice(equal + 1), enumerable: true, configurable: true, writable: true });
    }
    value.labels = labels;
  }
  for (const field of ['runId', 'subscriptionId', 'model', 'effort', 'workingDirectory', 'chatSessionId', 'serviceTier']) {
    if (value[field] !== undefined && (typeof value[field] !== 'string' || !value[field])) bad(`Invalid request field ${field}.`);
  }
  if (value.systemPrompt !== undefined && typeof value.systemPrompt !== 'string') bad('Invalid systemPrompt.');
  if (value.serviceTier !== undefined) bad('Claude does not offer a service tier setting.');
  const subscriptionId = value.subscriptionId as string ?? settings.defaultSubscription ?? 'anthropic';
  const defaults = settings.subscriptions?.[subscriptionId];
  const runId = value.runId as string ?? randomUUID();
  if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$/.test(runId)) bad('The run identifier is not a safe file name.');
  if (value.labels !== undefined && (!isObject(value.labels) || !Object.values(value.labels).every(item => typeof item === 'string'))) bad('Labels must be a string mapping.');
  identity?.(runId, value.labels as Record<string, string> | undefined);
  const tools = value.tools ?? (file === undefined ? 'none' : undefined);
  if (tools !== 'all' && tools !== 'none') bad('The request must name tools as all or none.');
  const history = value.history ?? (file === undefined ? [] : undefined);
  if (!Array.isArray(history)) bad('The request must contain a history array.');
  const validImages = (images: unknown): boolean => images === undefined || Array.isArray(images) && images.every(image => isObject(image) &&
    ['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(image.mediaType as string) && typeof image.data === 'string' &&
    image.data.length > 0 && image.data.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(image.data));
  for (const value of history as unknown[]) {
    if (!isObject(value)) bad('Each history item must be an object.');
    const item = value as Record<string, unknown>;
    if (item.type === 'message') {
      if (!['user', 'assistant'].includes(item.role as string) || typeof item.text !== 'string' || !validImages(item.images) ||
        item.role === 'assistant' && item.images !== undefined) bad('Invalid history message.');
    } else if (item.type === 'tool_call') {
      if (typeof item.name !== 'string' || !item.name || !('input' in item) || typeof item.ok !== 'boolean' ||
        item.error !== undefined && typeof item.error !== 'string') bad('Invalid history tool call.');
      if (item.name === 'shell' && (!isObject(item.input) || typeof item.input.command !== 'string')) bad('A shell history call needs a command.');
    } else bad('Unknown history item type.');
  }
  if (!isObject(value.input) || value.input.role !== 'user' || typeof value.input.text !== 'string') bad('The input must be a user message with text.');
  const input = value.input as RunRequest['input'];
  if (!validImages(input.images)) bad('Images must have supported media types and base64 data.');
  const strings = (field: string): string[] => {
    const list = value[field] ?? [];
    if (!Array.isArray(list) || !list.every(item => typeof item === 'string' && isAbsolute(item))) bad(`${field} must contain absolute paths.`);
    return list as string[];
  };
  const additionalDirectories = strings('additionalDirectories');
  const searchPath = strings('searchPath');
  let workingDirectory = value.workingDirectory as string | undefined;
  if (workingDirectory === undefined && tools === 'all') bad('A run with tools needs a working directory.');
  for (const path of [...(workingDirectory === undefined ? [] : [workingDirectory]), ...additionalDirectories]) {
    if (!isAbsolute(path) || !await stat(path).then(info => info.isDirectory()).catch(() => false)) bad('A working or additional directory does not exist.');
  }
  if (workingDirectory !== undefined) workingDirectory = await realpath(workingDirectory);
  if (value.limits !== undefined && (!isObject(value.limits) || Object.entries(value.limits).some(([key, n]) =>
    !['idleSeconds', 'totalSeconds'].includes(key) || typeof n !== 'number' || !Number.isFinite(n) || n < 0))) bad('Invalid run limits.');
  return { runId, subscriptionId, model: value.model as string ?? defaults?.defaultModel,
    effort: value.effort as string ?? defaults?.defaultEffort, systemPrompt: value.systemPrompt as string | undefined,
    workingDirectory, additionalDirectories, history: history as HistoryItem[], input, chatSessionId: value.chatSessionId as string | undefined,
    tools: tools as 'all' | 'none', labels: value.labels as Record<string, string> | undefined, searchPath, limits: value.limits as RunRequest['limits'] };
}
export function runArguments(request: RunRequest, conversation: string, control: boolean, resume = false): string[] {
  const args = ['-p', '--input-format', 'stream-json', '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
    '--permission-mode', 'bypassPermissions', '--system-prompt-snapshot', 'off', resume ? '--resume' : '--session-id', conversation];
  if (request.tools === 'none') args.push('--tools', '');
  else if (control) args.push('--permission-prompt-tool', 'stdio');
  if (request.workingDirectory === undefined) args.push('--no-session-persistence');
  for (const [option, value] of [['--model', request.model], ['--effort', request.effort], ['--append-system-prompt', request.systemPrompt]]) {
    if (value !== undefined) args.push(option!, value);
  }
  for (const directory of request.additionalDirectories) args.push('--add-dir', directory);
  return args;
}
export function userRecord(request: RunRequest): object {
  return { type: 'user', message: { role: 'user', content: [{ type: 'text', text: request.input.text },
    ...(request.input.images ?? []).map(image => ({ type: 'image', source: { type: 'base64', media_type: image.mediaType, data: image.data } }))] } };
}
