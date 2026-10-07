import { appendFile, mkdir, readFile, readlink, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';

export async function toolsStandIn(folder: string, args: string[], scenario: string, part: string): Promise<number> {
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const source = '~/Artifacts/arbol-claude-driver/research/probe-scripts-2026-10-06/live/subagent2.json';
  const output = (record: object): void => { process.stdout.write(JSON.stringify(record) + '\n'); };
  const log = async (stage: string, checks: object): Promise<void> => appendFile(join(folder, 'received.jsonl'), JSON.stringify({ scenario, part, stage,
    arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd(), checks }) + '\n');
  const option = (name: string): string | undefined => { const i = args.indexOf(name); return i < 0 ? undefined : args[i + 1]; };
  const message = (type: 'assistant' | 'user', content: object[], parent?: string): object => ({ type, parent_tool_use_id: parent ?? null, message: { role: type, content } });
  const text = (value: string, parent?: string): void => output(message('assistant', [{ type: 'text', text: value }], parent));
  const call = (id: string, name: string, input: object, parent?: string): void => output(message('assistant', [{ type: 'tool_use', id, name, input }], parent));
  const result = (id: string, content: unknown, error = false, parent?: string): void => output(message('user', [{ type: 'tool_result', tool_use_id: id, content, is_error: error }], parent));
  const final = (): void => output({ ...raw.result, is_error: false, result: 'answer' });
  const pending = new Map<string, string>(); const answered: string[] = [];
  const ask = (id: string, multiple = false): void => {
    const input = { questions: [{ question: 'Choose ' + id, header: id, multiSelect: multiple, options: [{ label: 'Blue', description: 'Cool' }, { label: 'Red', description: 'Warm' }] }] };
    call('call-' + id, 'AskUserQuestion', input); pending.set(id, 'call-' + id);
    output({ type: 'control_request', request_id: id, request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion', tool_use_id: 'call-' + id,
      requires_user_interaction: true, input } });
  };
  // Question/control envelopes are from the probe record, section 6 and its
  // 6 October trusted-mode addendum. Background envelopes are from source.
  // IDs, selected labels, error variants and timing are synthetic.
  const lines = createInterface({ input: process.stdin });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    for await (const line of lines) {
      const input = JSON.parse(line);
      if (input.type === 'control_request') {
        if (input.request.subtype === 'initialize') {
          const record = structuredClone(subscriptionProtocol.initialize); record.response.request_id = input.request_id; output(record);
        } else if (input.request.subtype === 'get_settings') output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id,
          response: { applied: { model: option('--model') ?? 'default', effort: option('--effort') ?? 'high' } } } });
        else if (input.request.subtype === 'interrupt') {
          await log('interrupt', { input });
          for (const id of pending.keys()) output({ type: 'control_cancel_request', request_id: id });
          pending.clear();
          output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: {} } });
          output({ ...raw.result, is_error: true, errors: ['aborted_tools'] });
        }
      } else if (input.type === 'control_response') {
        await log('response', { input });
        const id = input.response.request_id;
        const callId = pending.get(id);
        if (callId) { pending.delete(id); answered.push(id); result(callId, input.response.response, input.response.response.behavior === 'deny'); }
        if (scenario === 'C25') {
          if (id === 'allow') output({ type: 'control_request', request_id: 'deny', request: { subtype: 'can_use_tool' } });
          else { text('permission-completed'); final(); }
        } else if (pending.size === 0) { text('answers-' + answered.join('-')); final(); }
      } else if (input.type === 'user') {
        const skillsPath = process.env.CLAUDE_CONFIG_DIR! + '/skills';
        const skills = await readlink(join(skillsPath, 'fixture-skill')).catch(() => undefined);
        const mcpPath = option('--mcp-config');
        // The stand-in acts as Claude and reads only the named skill/MCP sources.
        const skill = skills ? await readFile(join(skillsPath, 'fixture-skill/SKILL.md'), 'utf8').catch(() => undefined) : undefined;
        const mcp = mcpPath ? JSON.parse(await readFile(mcpPath, 'utf8')).mcpServers : undefined;
        await log('input', { input, skills, skill, mcp, configHome: process.env.CLAUDE_CONFIG_DIR,
          memoryOff: process.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY === '1', source });
        output(raw.init);
        if (scenario === 'C9' || scenario === 'C37') {
          await mkdir(join(skillsPath, 'synced/test-account'), { recursive: true });
          await writeFile(join(skillsPath, 'synced/test-account/sentinel'), 'private account skill');
          text('owner-tools'); final();
        }
        else if (scenario === 'C25') {
          if (part === 'missing-id') output({ type: 'control_request', request: { subtype: 'can_use_tool', tool_name: 'Write', input: {} } });
          else output({ type: 'control_request', request_id: 'allow', request: { subtype: 'can_use_tool', tool_name: 'Write', input: { file_path: 'fixture', content: 'value' } } });
        } else if (scenario === 'C33') ask('unhandled');
        else if (scenario === 'C36') {
          if (part === 'decline') ask('decline');
          else {
            ask('first'); ask('second', true); ask('withdrawn');
            output({ type: 'control_cancel_request', request_id: 'withdrawn' }); pending.delete('withdrawn'); result('call-withdrawn', 'withdrawn', true);
          }
        } else if (scenario === 'C35') {
          call('refused', 'Bash', { command: 'rejected-command', cwd: process.cwd() }); result('refused', 'Claude refused this command.', true);
          call('agent', 'Agent', { prompt: 'synthetic sub-agent', model: 'haiku' }); result('agent', 'Agent launched');
          call('background', 'Bash', { command: 'synthetic-background', run_in_background: true }); result('background', 'Background command launched');
          output({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_id: 'agent-task', task_type: 'local_agent' }, { task_id: 'bash-task', task_type: 'local_bash' }] });
          text('main-before'); final();
          if (part === 'early-exit') { lines.close(); process.stdin.destroy(); return 0; }
          timer = setTimeout(() => {
            call('subcall', 'Bash', { command: 'echo sub-result' }, 'agent'); result('subcall', 'sub-result', false, 'agent'); text('sub-agent-only', 'agent');
            output({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_id: 'bash-task', task_type: 'local_bash' }] });
            text('main-after-agent'); final();
            timer = setTimeout(() => { void (async () => {
              output({ type: 'system', subtype: 'background_tasks_changed', tasks: [] });
              await log('background-ended', { ended: true }); text('main-after-command'); final();
            })(); }, 100);
          }, 100);
        }
      }
    }
    return 0;
  } finally { clearTimeout(timer); await log('stdin-closed', { pending: [...pending.keys()] }); }
}
