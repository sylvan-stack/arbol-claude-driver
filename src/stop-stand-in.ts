import { spawn } from 'node:child_process';
import { appendFile, mkdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { subscriptionProtocol } from './subscription-protocol.js';
export async function stopStandIn(folder: string, args: string[], scenario: string, part: string): Promise<number> {
  const raw = JSON.parse(await readFile(new URL('../../test/run-protocol-2026-10-06.json', import.meta.url), 'utf8'));
  const option = (key: string): string | undefined => args[args.indexOf(key) + 1];
  const output = (record: object): void => { process.stdout.write(JSON.stringify(record) + '\n'); };
  const log = async (stage: string, checks: object): Promise<void> => appendFile(join(folder, 'received.jsonl'), JSON.stringify({ scenario, part, stage,
    arguments: args, environment: Object.keys(process.env).sort(), workingDirectory: process.cwd(), checks }) + '\n');
  const text = (value: string): object => ({ ...raw.text, parent_tool_use_id: null, message: { ...raw.text.message, content: [{ type: 'text', text: value }] } });
  const call = (name: string, id: string, parent?: string): object => ({ ...raw.bash, parent_tool_use_id: parent ?? null,
    message: { ...raw.bash.message, content: [{ type: 'tool_use', id, name, input: { command: 'sleep 60' } }] } });
  const final = (stopped = false): object => ({ ...raw.result, is_error: stopped, result: stopped ? 'Interrupted' : 'answer' });
  // Envelope source: raw.source. Stop/question variants follow the recorded protocol
  // in tool-probes-2026-10-05.md sections 3 and 7 and its 6 October stop addendum.
  // Text, IDs, counts and delayed workers are synthetic; there is no vendor request.
  let heartbeat: ReturnType<typeof setInterval> | undefined;
  if (part === 'stubborn') process.on('SIGTERM', () => {});
  const lines = createInterface({ input: process.stdin });
  for await (const line of lines) {
    const input = JSON.parse(line);
    if (input.type === 'control_request') {
      if (input.request.subtype === 'initialize') {
        const record = structuredClone(subscriptionProtocol.initialize); record.response.request_id = input.request_id; output(record);
      } else if (input.request.subtype === 'get_settings') output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id,
        response: { applied: { model: option('--model') ?? 'default', effort: option('--effort') ?? 'high' } } } });
      else if (input.request.subtype === 'interrupt') {
        await log('interrupt', { input, source: '~/Artifacts/arbol-claude-driver/research/tool-probes-2026-10-05.md#7-stop' });
        if (part === 'stubborn') continue;
        if (part === 'subagent') output({ type: 'system', subtype: 'background_tasks_changed', tasks: [] });
        if (part === 'question') output({ type: 'control_cancel_request', request_id: 'fixture-ask' });
        output({ type: 'control_response', response: { subtype: 'success', request_id: input.request_id, response: { still_queued: [] } } });
        if (part !== 'subagent') { output(text('partial')); output(final(true)); }
      }
    } else if (input.type === 'user') {
      const session = option(args.includes('--resume') ? '--resume' : '--session-id')!;
      if (!args.includes('--no-session-persistence')) {
        const projects = join(process.env.CLAUDE_CONFIG_DIR!, 'projects', process.cwd().replace(/[^a-zA-Z0-9]/g, '-'));
        await mkdir(projects, { recursive: true }); await writeFile(join(projects, session + '.jsonl'), '{}\n');
      }
      output(raw.init);
      let backgroundPid: number | undefined;
      if (['background', 'hold', 'stubborn', 'subagent', 'linger'].includes(part)) {
        const path = join(folder, 'worker-' + process.pid);
        const worker = spawn(process.env.CLAUDE_CODE_SHELL!, ['-c', `sleep 60 >/dev/null 2>&1 & echo $! > '${path}'`], { detached: true, stdio: 'ignore', env: process.env });
        await new Promise<void>((resolve, reject) => { worker.once('error', reject); worker.once('exit', () => resolve()); });
        backgroundPid = Number((await readFile(path, 'utf8')).trim());
      }
      let backgroundWasAlive = false;
      if (backgroundPid) { try { process.kill(backgroundPid, 0); backgroundWasAlive = true; } catch {} }
      await log('input', { input, toolPid: process.pid, backgroundPid, backgroundWasAlive, shell: process.env.CLAUDE_CODE_SHELL, source: raw.source, synthetic: 'stop variants' });
      output({ type: 'stream_event', event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'partial' } } });
      if (part === 'tool' || part === 'question') output(call(part === 'tool' ? 'Bash' : 'AskUserQuestion', 'fixture-call'));
      if (part === 'question') output({ type: 'control_request', request_id: 'fixture-ask', request: { subtype: 'can_use_tool', tool_name: 'AskUserQuestion', tool_use_id: 'fixture-call',
        input: { questions: [{ question: 'Choose a colour', header: 'Colour', multiSelect: false, options: [{ label: 'Blue', description: 'Blue' }, { label: 'Red', description: 'Red' }] }] } } });
      if (part === 'subagent') {
        output(call('Agent', 'fixture-agent'));
        output({ ...raw.toolResult, message: { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'fixture-agent', content: 'Agent launched' }] } });
        output({ type: 'system', subtype: 'background_tasks_changed', tasks: [{ task_id: 'fixture-task', task_type: 'local_agent', description: 'worker' }] });
        output(final()); output(call('Bash', 'fixture-subcall', 'fixture-agent'));
        heartbeat = setInterval(() => { void log('subagent-tick', { active: true }); }, 20);
      } else if (['plain', 'background', 'linger'].includes(part)) { output(text('answer')); output(final()); }
    }
  }
  clearInterval(heartbeat);
  if (part === 'linger') await new Promise<void>(() => { setInterval(() => {}, 60000); });
  return 0;
}
