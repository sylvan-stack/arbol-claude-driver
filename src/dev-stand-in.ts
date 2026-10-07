import { appendFile, mkdir, readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';

// Control and result envelopes from the 2.1.289 probes; IDs, text and failures are synthetic.
export async function devStandIn(folder: string, args: string[], scenario: string, part: string): Promise<number> {
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const option = (name: string): string | undefined => { const index = args.indexOf(name); return index < 0 ? undefined : args[index + 1]; };
  const output = (record: object) => process.stdout.write(JSON.stringify(record) + '\n');
  const log = (stage: string, checks: object) => appendFile(join(folder, 'received.jsonl'), JSON.stringify({ scenario, part, stage, checks,
    arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd() }) + '\n');
  if (args.includes('--arbol-unknown-option')) { process.stderr.write('error: unknown option --arbol-unknown-option\n'); return 1; }
  const resumed = option('--resume');
  if (resumed) {
    const projects = join(process.env.CLAUDE_CONFIG_DIR!, 'projects');
    const directories = await readdir(projects).catch(() => []);
    const files = await Promise.all(directories.map(directory => readFile(join(projects, directory, resumed + '.jsonl')).then(() => true).catch(() => false)));
    if (!files.includes(true)) { output({ type: 'result', subtype: 'error_during_execution', is_error: true, errors: ['No conversation found'] }); return 1; }
  }
  const lines = createInterface({ input: process.stdin });
  const final = (content: string) => output({ ...raw.result, is_error: false, result: content });
  for await (const line of lines) {
    const input = JSON.parse(line);
    if (input.type === 'control_request') {
      await log('control', { subtype: input.request.subtype, source: subscriptionProtocol.source });
      let response: object = {};
      if (input.request.subtype === 'initialize') response = { ...structuredClone(subscriptionProtocol.initialize.response.response), current_permission_mode: 'bypassPermissions' };
      if (input.request.subtype === 'get_settings') response = { applied: { model: option('--model') ?? 'default', effort: option('--effort') ?? 'high' } };
      if (input.request.subtype === 'mcp_status') response = { mcpServers: [] };
      output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response } });
      if (input.request.subtype === 'interrupt') output({ ...raw.result, is_error: true });
    } else if (input.type === 'control_response' && part === 'live') {
      await log('answer', { delivered: true });
      output({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'ask-call', content: 'Ready' }] } });
      output({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'shell-call', name: 'Bash', input: { command: 'printf ready' } }] } });
      output({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'shell-call', content: 'ready' }] } });
      output({ ...raw.text, message: { content: [{ type: 'text', text: 'ready' }] } }); final('ready');
    } else if (input.type === 'user') {
      await log('input', { input, source: raw.source, memoryOff: process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY === '1' });
      output(raw.init);
      if (part === 'live') {
        const prompt = input.message.content[0].text;
        if (prompt.includes('AskUserQuestion')) {
          const questions = [{ question: 'Ready?', header: 'Ready', multiSelect: false, options: [{ label: 'Ready' }, { label: 'Later' }] }];
          output({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'ask-call', name: 'AskUserQuestion', input: { questions } }] } });
          output({ type: 'control_request', request_id: 'ask', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion', tool_use_id: 'ask-call', input: { questions } } });
          continue;
        }
        if (prompt.includes('sleep 8')) {
          output({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'stopping-call', name: 'Bash', input: { command: 'sleep 8' } }] } }); continue;
        }
        if (prompt.includes('sub-agent')) {
          output({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_id: 'background', task_type: 'local_agent' }] });
          output({ ...raw.result, is_error: false });
          output({ ...raw.text, parent_tool_use_id: 'background-call', message: { content: [{ type: 'text', text: 'sub-agent ready' }] } });
          output({ type: 'system', subtype: 'background_tasks_changed', tasks: [] });
        }
        if (!args.includes('--no-session-persistence')) {
          const projects = join(process.env.CLAUDE_CONFIG_DIR!, 'projects', process.cwd().replace(/[^a-zA-Z0-9]/g, '-'));
          await mkdir(projects, { recursive: true }); await writeFile(join(projects, (option('--resume') ?? option('--session-id')) + '.jsonl'), '{}\n');
        }
        const answer = prompt.includes('What word') ? 'willow' : 'ready';
        output({ ...raw.text, message: { content: [{ type: 'text', text: answer }] } }); final(answer); continue;
      }
      const secret = scenario === 'C7' ? process.env.ANTHROPIC_AUTH_TOKEN ?? '' : '';
      const content = secret || (part === 'build-failed' ? 'other' : 'ready');
      output({ type: 'stream_event', event: { type: 'content_block_delta', delta: { type: 'text_delta', text: content } } });
      output({ ...raw.text, message: { ...raw.text.message, content: [{ type: 'text', text: content }, { type: 'thinking', thinking: content }] } });
      if (scenario === 'C7') {
        output({ type: 'assistant', message: { content: [{ type: 'tool_use', id: 'secret-call', name: 'Read', input: { value: secret } }] } });
        output({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'secret-call', content: { value: secret }, is_error: true }] } });
        if (part === 'stderr') { process.stderr.write('Synthetic failure: ' + secret); lines.close(); process.stdin.destroy(); return 1; }
      }
      output({ ...raw.result, is_error: part === 'failed', result: content,
        ...(part === 'failed' ? { api_error_status: 400, errors: [content] } : {}),
        ...(secret ? { modelUsage: { [secret]: { costBasis: 'unknown' } } } : {}) });
    }
  }
  return 0;
}
