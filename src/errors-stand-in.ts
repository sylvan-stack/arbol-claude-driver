import { appendFile, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';

export async function errorsStandIn(folder: string, args: string[], scenario: string, part: string): Promise<number> {
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const errors = JSON.parse(await readFile(new URL('../../test/errors-probe-2026-10-07.json', import.meta.url), 'utf8'));
  const limits = JSON.parse(await readFile(new URL('../../test/errors-protocol-2026-10-07.json', import.meta.url), 'utf8'));
  const output = (record: object): void => { process.stdout.write(JSON.stringify(record) + '\n'); };
  const log = (stage: string, checks: object = {}): Promise<void> => appendFile(join(folder, 'received.jsonl'), JSON.stringify({ scenario, part, stage, checks,
    arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd() }) + '\n');
  const final = (fields: object = {}): void => output({ ...raw.result, is_error: false, ...fields });
  const failed = (code?: string, status?: number, message = 'Synthetic vendor refusal', fields: object = {}): void => {
    if (code) output({ ...errors.rawLoginFailure.assistant, error: code, message: { content: [{ type: 'text', text: message }] } });
    final({ is_error: true, api_error_status: status ?? null, result: message, ...fields });
  };
  const call = (name: string): void => output({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'waiting-call', name, input: { command: 'synthetic-work' } }] } });
  const finishedCall = (): void => output({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'waiting-call', content: 'done' }] } });
  const limit = (value: number): void => output({ ...limits.rateLimit, rate_limit_info: { ...limits.rateLimit.rate_limit_info,
    rateLimitType: 'seven_day', unifiedWindows: { five_hour: { utilization: value, resetsAt: 1900000000 }, seven_day: { utilization: value * 4, resetsAt: 1900010000 } } } });
  const lines = createInterface({ input: process.stdin });
  let timer: ReturnType<typeof setTimeout> | undefined, heartbeat: ReturnType<typeof setInterval> | undefined;
  const after = (fn: () => void): void => { timer = setTimeout(fn, 450); };
  try {
    for await (const line of lines) {
      const input = JSON.parse(line);
      if (input.type === 'control_request') {
        if (input.request.subtype === 'initialize') { const record = structuredClone(subscriptionProtocol.initialize); record.response.request_id = input.request_id; output(record); }
        else if (input.request.subtype === 'get_settings') output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: { applied: {} } } });
        else if (input.request.subtype === 'interrupt') {
          await log('interrupt'); clearTimeout(timer); clearInterval(heartbeat);
          output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: {} } }); final({ is_error: true, errors: ['Interrupted'] });
        }
      } else if (input.type === 'control_response') { await log('answered'); finishedCall(); final(); }
      else if (input.type === 'user') {
        await log('input', { source: 'test/errors-probe-2026-10-07.json', limitSource: limits.source,
          synthetic: 'Codes, statuses, waits, utilization and timing are synthetic variants of recorded envelopes', maxRetries: process.env.CLAUDE_CODE_MAX_RETRIES });
        if (scenario === 'C29' && part === 'start') continue;
        output(raw.init);
        if (scenario === 'C4') failed('authentication_failed', 401, 'Failed to authenticate.');
        else if (scenario === 'C26') {
          if (part === 'authentication') failed('authentication_failed', undefined, 'Not logged in · Please run /login');
          else if (part === '401' || part === '403') failed(undefined, Number(part), 'quota exceeded but the credential was rejected');
          else if (part === 'model') failed('model_not_found', 404);
          else if (part === '404') failed(undefined, 404);
          else if (part === 'rate') failed('rate_limit_error');
          else if (part === '429') failed(undefined, 429);
          else if (part === 'server') failed('server_error');
          else if (part === '503') failed(undefined, 503);
          else if (part === 'context') failed(undefined, 400, 'The conversation is too long.');
          else if (part === '400' || part === '418') failed(undefined, Number(part), 'quota exceeded but another refusal was stated');
          else if (part === 'other-code') failed('unrecognised_refusal', undefined, 'rate limit mentioned in an unrelated refusal');
          else if (part === 'turns') failed(undefined, undefined, 'Too many turns', { subtype: 'error_max_turns' });
          else if (part === 'wording-rate') failed(undefined, undefined, 'Usage limit; retry after 5 minutes');
          else if (part === 'wording-auth') failed(undefined, undefined, 'Invalid authentication');
          else if (part === 'missing' || part === 'argument' || part === 'exit') {
            process.stderr.write(part === 'missing' ? 'exec: claude: not found\n' : part === 'argument' ? 'error: unknown option --future\n' : 'Synthetic tool failure\n');
            lines.close(); process.stdin.destroy(); return part === 'missing' ? 127 : 1;
          } else failed();
        } else if (scenario === 'C27') {
          if (part === 'reset') limit(0.25);
          failed(undefined, 429, part === 'minutes' ? 'Retry after 5 minutes' : part === 'seconds' ? 'Try again in 8 seconds' : 'Account limit reached');
        } else if (scenario === 'C28') {
          output({ ...errors.rawRejectedKey.retries[0], max_retries: 3 });
          after(() => { void log('second-retry'); output({ type: 'system', subtype: 'api_retry', attempt: 2, error_status: 401 }); failed('authentication_failed', 401); });
        } else if (scenario === 'C29') {
          if (part === 'call' || part === 'total') { call('Bash'); after(() => { finishedCall(); final(); }); }
          else if (part === 'background') { output({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_type: 'local_bash', task_id: 'waiting-task' }] }); final();
            after(() => { output({ type: 'system', subtype: 'background_tasks_changed', tasks: [] }); final(); }); }
          else if (part === 'question') {
            call('AskUserQuestion'); output({ type: 'control_request', request_id: 'waiting', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion', tool_use_id: 'waiting-call',
              input: { questions: [{ question: 'Choose', options: [{ label: 'Blue' }, { label: 'Red' }] }] } } });
          } else if (part === 'heartbeat') { heartbeat = setInterval(() => output({ type: 'system', subtype: 'thinking_tokens' }), 60); after(() => { clearInterval(heartbeat); final(); }); }
          else if (part === 'idle-off') after(() => final());
        } else if (scenario === 'C30') { limit(0.1); limit(0.25); final(); }
      }
    }
    return 0;
  } finally { clearTimeout(timer); clearInterval(heartbeat); }
}
