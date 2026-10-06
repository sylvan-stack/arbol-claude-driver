import { randomUUID } from 'node:crypto';
import type { Writable } from 'node:stream';
import { Failure, safeFailure } from './errors.js';
import type { DriverError } from './errors.js';

export interface RunUsage { model?: string; inputTokens?: number; cachedInputTokens?: number; outputTokens?: number; reasoningTokens?: number; costUsd?: number }
export type EventBody = { type: string; [key: string]: unknown };
export class RunEvents {
  runId = randomUUID() as string;
  labels: Record<string, string> | undefined;
  seq = 0;
  finished = false;
  private status = 1;
  onText: ((text: string) => void) | undefined;
  producedOutput = false;
  usage: RunUsage = {};
  readonly calls = new Map<string, { name: string; parentCallId?: string }>();
  readonly asks = new Set<string>();
  constructor(readonly output: Writable = process.stdout, readonly textOnly = false) {}
  async emit(event: EventBody): Promise<void> {
    if (this.finished) return;
    const record = { runId: this.runId, seq: ++this.seq, at: new Date().toISOString(), ...(this.labels ? { labels: this.labels } : {}), ...event };
    if ((event.type === 'text' || event.type === 'text_delta' || event.type === 'thinking' || event.type === 'thinking_delta') && event.text) this.producedOutput = true;
    if (event.type === 'tool_started') this.producedOutput = true;
    if (event.type === 'text' && !event.parentCallId) this.onText?.(String(event.text));
    if (event.type === 'finished') this.finished = true;
    const text = this.textOnly ? event.type === 'text' && !event.parentCallId ? String(event.text) : '' : JSON.stringify(record) + '\n';
    if (text) await new Promise<void>((resolve, reject) => this.output.write(text, error => error ? reject(error) : resolve()));
  }
  async finish(error?: unknown, cancelled = false): Promise<number> {
    if (this.finished) return cancelled ? 2 : this.status;
    const unfinishedCalls = [...this.calls.keys()];
    for (const askId of this.asks) await this.emit({ type: 'ask_closed', askId, reason: 'withdrawn' });
    this.asks.clear();
    for (const [callId, call] of this.calls) await this.emit({ type: 'tool_finished', callId, ok: false,
      error: 'The run ended before the tool call finished.', interrupted: true, ...(call.parentCallId ? { parentCallId: call.parentCallId } : {}) });
    this.calls.clear();
    const failure: DriverError | undefined = cancelled ? undefined : error === undefined ? undefined : safeFailure(error);
    this.status = cancelled ? 2 : failure ? 1 : 0;
    await this.emit({ type: 'finished', outcome: cancelled ? 'cancelled' : failure ? 'failed' : 'completed',
      ...(failure ? { error: failure } : {}), ...(Object.keys(this.usage).length ? { usage: this.usage } : {}),
      producedOutput: this.producedOutput, unfinishedCalls });
    return this.status;
  }
}
export function truncateResult(result: unknown): { result: unknown; truncated?: true } {
  const text = typeof result === 'string' ? result : JSON.stringify(result);
  if (text === undefined) return { result };
  const bytes = Buffer.from(text);
  if (bytes.length <= 200 * 1024) return { result };
  let end = 200 * 1024;
  while ((bytes[end]! & 0xc0) === 0x80) end--;
  return { result: bytes.subarray(0, end).toString('utf8'), truncated: true };
}
export function tokenUsage(value: unknown): RunUsage {
  if (value === null || typeof value !== 'object') return {};
  const raw = value as Record<string, any>;
  const number = (n: unknown): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0;
  const input = ['input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'].map(key => raw[key]).filter(number);
  return { ...(input.length ? { inputTokens: input.reduce((a, b) => a + b, 0) } : {}),
    ...(number(raw.cache_read_input_tokens) ? { cachedInputTokens: raw.cache_read_input_tokens } : {}),
    ...(number(raw.output_tokens) ? { outputTokens: raw.output_tokens } : {}),
    ...(number(raw.output_tokens_details?.thinking_tokens) ? { reasoningTokens: raw.output_tokens_details.thinking_tokens } : {}) };
}
export function addUsage(a: RunUsage, b: RunUsage): RunUsage {
  const total: RunUsage = { ...a, ...(b.model ? { model: b.model } : {}) };
  for (const key of ['inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningTokens'] as const) {
    if (b[key] !== undefined) total[key] = (a[key] ?? 0) + b[key]!;
  }
  return total;
}
export const protocolFailure = (): Failure => new Failure('tool_unsupported', 'Claude returned a malformed protocol record.');
