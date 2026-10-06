import type { Continuity } from './continuity.js';
import { Failure } from './errors.js';
import { isObject } from './settings.js';
import { addUsage, tokenUsage, truncateResult, protocolFailure } from './run-events.js';
import type { RunEvents, RunUsage } from './run-events.js';
import type { RunRequest } from './run-request.js';

export class RunProtocol {
  started = false;
  result?: Record<string, any>;
  error: Failure | undefined;
  background = false;
  private readonly messages = new Map<string, RunUsage>();
  private finalUsage: RunUsage = {};
  constructor(readonly events: RunEvents, readonly request: RunRequest, readonly build: string, readonly version: string,
    readonly family: string, readonly send: (record: object) => Promise<void>, readonly continuity: Pick<Continuity, 'continuity' | 'rebuildReason' | 'textForm'> = { continuity: 'fresh', textForm: false }, readonly control = false) {}
  async accept(value: unknown): Promise<void> {
    if (!isObject(value) || typeof value.type !== 'string') throw protocolFailure();
    const record = value as Record<string, any>;
    const parent = typeof record.parent_tool_use_id === 'string' && record.parent_tool_use_id ? { parentCallId: record.parent_tool_use_id } : {};
    const emit = async (event: { type: string; [key: string]: unknown }): Promise<void> => { await this.events.emit({ ...event, ...parent }); };
    switch (record.type) {
      case 'system':
        if (record.subtype === 'init' && !this.started) {
          this.started = true;
          await emit({ type: 'started', continuity: this.continuity.continuity, ...(this.continuity.rebuildReason ? { rebuildReason: this.continuity.rebuildReason } : {}), subscriptionId: this.request.subscriptionId,
            ...(typeof record.model === 'string' ? { model: record.model } : {}), ...(this.request.effort ? { effort: this.request.effort } : {}),
            toolVersion: record.claude_code_version ?? this.version, driverBuild: this.build });
          if (this.continuity.textForm) await emit({ type: 'notice', code: 'history_as_text', message: 'The earlier conversation was sent as labelled text because no native session could be used.' });
        } else if (record.subtype === 'api_retry') await emit({ type: 'notice', code: 'api_retry', message: `Claude is retrying (attempt ${record.attempt ?? record.attempt_number ?? 'unknown'}).` });
        else if (record.subtype === 'background_tasks_changed') {
          if (!Array.isArray(record.tasks)) throw protocolFailure();
          this.background = record.tasks.length > 0;
        }
        break;
      case 'stream_event': {
        if (!isObject(record.event) || typeof record.event.type !== 'string') throw protocolFailure();
        const event = record.event;
        const key = parent.parentCallId ?? 'main';
        if (event.type === 'content_block_delta') {
          if (!isObject(event.delta)) throw protocolFailure();
          const delta = event.delta;
          if (delta.type === 'text_delta' && typeof delta.text === 'string') await emit({ type: 'text_delta', text: delta.text });
          if (delta.type === 'thinking_delta' && typeof delta.thinking === 'string' && delta.thinking) await emit({ type: 'thinking_delta', text: delta.thinking });
        } else if (event.type === 'message_start') {
          if (!isObject(event.message)) throw protocolFailure();
          this.messages.set(key, { ...tokenUsage(event.message.usage), ...(typeof event.message.model === 'string' ? { model: event.message.model } : {}) });
        } else if (event.type === 'message_delta') {
          this.messages.set(key, { ...this.messages.get(key), ...tokenUsage(event.usage) });
        } else if (event.type === 'message_stop') {
          const usage = this.messages.get(key);
          if (usage) { this.events.usage = addUsage(this.events.usage, usage); this.messages.delete(key); await this.events.emit({ type: 'usage', usage: this.events.usage }); }
        }
        break;
      }
      case 'assistant':
        if (!isObject(record.message) || !Array.isArray(record.message.content)) throw protocolFailure();
        if (record.error) {
          this.error = new Failure('vendor_error', record.message.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n'));
          break;
        }
        for (const block of record.message.content) {
          if (!isObject(block)) throw protocolFailure();
          if (block.type === 'text' || block.type === 'thinking') {
            const text = block.type === 'text' ? block.text : block.thinking;
            if (typeof text !== 'string') throw protocolFailure();
            if (text) await emit({ type: block.type, text });
          } else if (block.type === 'tool_use') {
            if (typeof block.id !== 'string' || !block.id || typeof block.name !== 'string' || !('input' in block) || this.events.calls.has(block.id)) throw protocolFailure();
            this.events.calls.set(block.id, { name: block.name, ...parent });
            await emit({ type: 'tool_started', callId: block.id, name: block.name === 'Bash' ? 'shell' : block.name,
              input: block.name === 'Bash' ? { command: isObject(block.input) ? block.input.command : undefined } : block.input });
          }
        }
        break;
      case 'user':
        if (!isObject(record.message) || !Array.isArray(record.message.content)) throw protocolFailure();
        for (const block of record.message.content) {
          if (!isObject(block) || block.type !== 'tool_result') continue;
          if (typeof block.tool_use_id !== 'string' || !this.events.calls.has(block.tool_use_id)) throw protocolFailure();
          const call = this.events.calls.get(block.tool_use_id)!;
          this.events.calls.delete(block.tool_use_id);
          const result = call.name === 'Bash' ? { output: typeof block.content === 'string' ? block.content : JSON.stringify(block.content) } : block.content;
          await emit({ type: 'tool_finished', callId: block.tool_use_id, ok: !block.is_error, ...truncateResult(result),
            ...(block.is_error ? { error: 'The tool call failed.' } : {}) });
        }
        break;
      case 'control_request':
        if (typeof record.request_id !== 'string' || !record.request_id || !isObject(record.request)) throw protocolFailure();
        if (this.control && record.request.subtype === 'can_use_tool' && record.request.tool_name === 'AskUserQuestion') {
          const input = record.request.input;
          if (!isObject(input) || !Array.isArray(input.questions) || typeof record.request.tool_use_id !== 'string' || this.events.asks.has(record.request_id)) throw protocolFailure();
          const questions = input.questions.map((question: any) => {
            if (!isObject(question) || typeof question.question !== 'string' || !Array.isArray(question.options) || !question.options.every((option: any) => isObject(option) && typeof option.label === 'string')) throw protocolFailure();
            return { id: question.question, question: question.question, ...(typeof question.header === 'string' ? { header: question.header } : {}), multiple: question.multiSelect === true, options: question.options };
          });
          this.events.asks.add(record.request_id);
          await this.events.emit({ type: 'ask_opened', askId: record.request_id, ask: { callId: record.request.tool_use_id, questions } });
          break;
        }
        await this.send({ type: 'control_response', response: { subtype: 'error', request_id: record.request_id,
          error: record.request.subtype === 'can_use_tool' && record.request.tool_name === 'AskUserQuestion'
            ? 'This run has no question handler.' : 'Unknown control request.' } });
        break;
      case 'result': {
        if (typeof record.is_error !== 'boolean') throw protocolFailure();
        this.result = record;
        this.finalUsage = addUsage(this.finalUsage, tokenUsage(record.usage));
        const models = isObject(record.modelUsage) ? Object.entries(record.modelUsage) : [];
        if (models.length) this.finalUsage.model = models.at(-1)![0];
        if (this.family === 'anthropic' && typeof record.total_cost_usd === 'number' && Number.isFinite(record.total_cost_usd) && record.total_cost_usd >= 0 &&
          models.length > 0 && models.every(([, model]) => isObject(model) && model.costBasis === 'list')) this.finalUsage.costUsd = record.total_cost_usd;
        else delete this.finalUsage.costUsd;
        this.events.usage = { ...this.finalUsage };
        await this.events.emit({ type: 'usage', usage: this.events.usage });
        if (record.is_error) this.error ??= new Failure('vendor_error', (Array.isArray(record.errors) ? record.errors.join('\n') : record.result) || 'Claude reported an error.');
        else this.error = undefined;
        break;
      }
      case 'control_cancel_request':
        if (this.events.asks.delete(record.request_id)) await this.events.emit({ type: 'ask_closed', askId: record.request_id, reason: 'withdrawn' });
        break;
      case 'rate_limit_event': case 'control_response': break;
      default: throw protocolFailure();
    }
  }
}
