import { continuityStandIn } from './continuity-stand-in.js';
import { readFile, appendFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';
export const runScenarios = ['C10', 'C11', 'C12', 'C13', 'C14', 'C18', 'C19', 'C20', 'C33', 'C40', 'C42', 'C43'];
export async function runStandIn(folder: string, args: string[], scenario: string, part: string): Promise<number> {
  if (scenario === 'C14') return continuityStandIn(folder, args, part);
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const output = (record: object): void => { process.stdout.write(JSON.stringify(record) + '\n'); };
  const option = (name: string): string | undefined => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };
  const common = { scenario, part, arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd() };
  const text = (value: string): object => ({ ...structuredClone(raw.text), parent_tool_use_id: null, message: { ...raw.text.message, content: [{ type: 'text', text: value }] } });
  const delta = (value: string): object => ({ type: 'stream_event', parent_tool_use_id: null, event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: value } } });
  const tool = (): object => ({ ...structuredClone(raw.bash), parent_tool_use_id: null, message: { ...raw.bash.message,
    content: [{ type: 'tool_use', id: 'fixture-call', name: part === 'structure' ? 'Read' : 'Bash', input: { command: 'printf fixture-output' } }] } });
  const result = (content: unknown): object => ({ ...structuredClone(raw.toolResult), parent_tool_use_id: null,
    message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'fixture-call', content }] } });
  const final = (failed = false): object => ({ ...structuredClone(raw.result),
    ...(process.env.ANTHROPIC_BASE_URL ? { modelUsage: { [option('--model') ?? 'glm-fixture']: { costBasis: 'unknown' } } } : {}),
    is_error: failed, result: failed ? 'Synthetic vendor failure with a success subtype.' : 'answer' });
  // Record shapes originate in raw.source. Input text, counts and failure variants are synthetic.
  const lines = createInterface({ input: process.stdin });
  for await (const line of lines) {
    const input = JSON.parse(line);
    if (input.type === 'control_request') {
      if (input.request.subtype === 'initialize') {
        const record = structuredClone(subscriptionProtocol.initialize); record.response.request_id = input.request_id; output(record);
      } else if (input.request.subtype === 'get_settings') {
        output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: {
          applied: { model: option('--model') ?? 'default', effort: part === 'dropped-effort' ? null : option('--effort') ?? 'high' } } } });
      } else if (input.request.subtype === 'interrupt') {
        output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: {} } });
        output(final(true)); return 0;
      }
    } else if (input.type === 'user') {
      await appendFile(join(folder, 'received.jsonl'), JSON.stringify({ ...common, stage: 'input', checks: { source: raw.source, toolVersion: raw.toolVersion,
        input, autoMemoryDisabled: process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY === '1', streamArguments: args.includes('--verbose'),
        noSessionPersistence: args.includes('--no-session-persistence') } }) + '\n');
      const message = input.message.content[0].text;
      if (part === 'malformed') { output({ type: 'control_request', request: { subtype: 'unknown' } }); continue; }
      output({ ...raw.init, model: process.env.ANTHROPIC_BASE_URL ? option('--model') ?? 'glm-fixture' : option('--model') === 'sonnet' ? 'claude-sonnet-5-5' : option('--model') === 'opus' ? 'claude-opus-5-5' : 'claude-haiku-4-5-20251001' });
      if (part === 'hold' || message === 'hold') { await new Promise(resolve => setTimeout(resolve, 1200)); }
      if (scenario === 'C18' && message === 'slow') {
        for (let i = 0; i < 32; i++) output(text('s'.repeat(64 * 1024)));
      } else { output(delta('an')); output(text('answer')); }
      if (scenario === 'C42') {
        output({ type: 'stream_event', event: { type: 'message_start', message: { usage: { input_tokens: 3, output_tokens: 2 } } } });
        output({ type: 'stream_event', event: { type: 'message_stop' } });
      }
      if (scenario === 'C11' || scenario === 'C20' || scenario === 'C42') {
        output(tool());
        if (scenario !== 'C42') {
          output(result(scenario === 'C20' ? part === 'structure' ? { nested: ['😀'.repeat(60000)] } : '😀'.repeat(60000) : 'fixture-output'));
          output(text('after-tool'));
        }
      }
      if (scenario === 'C13') {
        output({ type: 'stream_event', event: { type: 'message_start', message: { model: raw.init.model, usage: { input_tokens: 3 } } } });
        output({ type: 'stream_event', event: { type: 'message_delta', usage: { output_tokens: 2 } } });
        output({ type: 'stream_event', event: { type: 'message_stop' } });
        output({ type: 'stream_event', event: { type: 'message_start', message: { model: raw.init.model, usage: { input_tokens: 4 } } } });
        output({ type: 'stream_event', event: { type: 'message_delta', usage: { output_tokens: 3 } } });
        output({ type: 'stream_event', event: { type: 'message_stop' } });
        const record = final() as any; record.usage = { input_tokens: 7, output_tokens: 5 }; record.modelUsage = { [raw.init.model]: { costBasis: 'unknown' } }; delete record.total_cost_usd; output(record);
      } else if (scenario === 'C42') { lines.close(); process.stdin.destroy(); return 1; }
      else output(final(scenario === 'C12' || message === 'fail'));
    }
  }
  return 0;
}
